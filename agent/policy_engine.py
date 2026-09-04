"""Stage 2 of the pipeline: the deterministic policy engine.

This module contains NO LLM calls and NO randomness. Given an event and the
classifier's category, it decides the single concrete action to take, and when.

It is the home of every hard guardrail:

  1. Hard-decline denylist  -- failure reasons that must never be retried,
     enforced regardless of what the classifier said.
  2. Retry cap              -- never more than config.MAX_RETRY_ATTEMPTS total.
  3. Cooldown               -- never a retry within config.COOLDOWN_HOURS of the
     previous attempt.
  4. Escalation             -- when the cap is hit (or the category is
     unrecognized), route to a human instead of looping or silently giving up.

Because these are plain Python checks, a wrong or adversarial classification
cannot cause a runaway retry or a retry on a blocked card. That containment is
the point of splitting this out from the classifier.

Every decision carries `rules_applied` -- an ordered list of the rule ids that
fired -- so the audit log shows exactly why each action was chosen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from agent import config
from agent.classifier import Classification


@dataclass(frozen=True)
class Decision:
    """The policy engine's output for a single event + classification."""

    action: str                       # one of config.ACTIONS
    retry_delay_hours: float | None    # set only for smart_retry
    scheduled_retry_at: str | None     # ISO 8601, set only for smart_retry
    rationale: str                     # plain-language "why this action"
    rules_applied: list                # ordered rule ids that fired
    guardrail_triggered: bool          # True if a hard limit forced the outcome

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Smart-retry timing helpers
# ---------------------------------------------------------------------------

def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _salary_slot_in(window_start: datetime, window_end: datetime) -> datetime | None:
    """Return the earliest 1st-5th @ SALARY_RETRY_HOUR inside [start, end], or None.

    Checks this month and the next two (a window can straddle a month boundary).
    """
    candidates: list[datetime] = []
    for month_offset in (0, 1, 2):
        year = window_start.year + (window_start.month - 1 + month_offset) // 12
        month = (window_start.month - 1 + month_offset) % 12 + 1
        for day in config.SALARY_CREDIT_DAYS:
            slot = datetime(year, month, day, config.SALARY_RETRY_HOUR)
            if window_start <= slot <= window_end:
                candidates.append(slot)
    return min(candidates) if candidates else None


def _schedule_retry(event: dict, reason: str) -> tuple[float, str, str]:
    """Compute (delay_hours, scheduled_iso, timing_note) for a smart_retry.

    Transient failures (network_error, bank_timeout): fixed short delay, floored
    at the cooldown. insufficient_funds: 24-48h out, snapped onto a salary-credit
    day (1st-5th) if one falls within the 24-72h window.
    """
    last_failure = _parse_ts(event["timestamp"])
    cooldown_floor = last_failure + timedelta(hours=config.COOLDOWN_HOURS)

    if reason in config.TRANSIENT_RETRY_HOURS:
        base_hours = config.TRANSIENT_RETRY_HOURS[reason]
        scheduled = max(last_failure + timedelta(hours=base_hours), cooldown_floor)
        note = (
            f"Transient failure ({reason}); retry after a {base_hours}h delay "
            f"(cooldown floor {config.COOLDOWN_HOURS}h)."
        )
        return _hours_between(last_failure, scheduled), scheduled.isoformat(), note

    # insufficient_funds (and any other soft reason without a fixed delay)
    if last_failure.day in (28, 29, 30, 31, 1, 2, 3):
        # Salary likely just credited or is about to -- 24h is enough.
        scheduled = last_failure + timedelta(hours=24)
    else:
        scheduled = last_failure + timedelta(hours=48)

    snap = _salary_slot_in(
        last_failure + timedelta(hours=24),
        last_failure + timedelta(hours=72),
    )
    if snap is not None:
        scheduled = snap
        note = (
            "Insufficient funds; retry snapped onto the next salary-credit day "
            f"({snap:%Y-%m-%d} {config.SALARY_RETRY_HOUR:02d}:00) within the 24-72h window."
        )
    else:
        note = (
            "Insufficient funds; retry scheduled "
            f"{_hours_between(last_failure, scheduled):.0f}h out "
            "(no salary-credit day in the 24-72h window)."
        )

    scheduled = max(scheduled, cooldown_floor)
    return _hours_between(last_failure, scheduled), scheduled.isoformat(), note


def _hours_between(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 3600.0, 1)


# ---------------------------------------------------------------------------
# The policy engine
# ---------------------------------------------------------------------------

def decide(
    event: dict,
    classification: Classification,
    *,
    attempt_index: int = 0,
) -> Decision:
    """Decide the single action for one event.

    `attempt_index` is how many recovery attempts have already been made in THIS
    run (the executor increments it as it loops). Total lifetime attempts =
    event['retry_count_so_far'] + attempt_index, and that total is what the
    retry cap is checked against.
    """
    reason = event["failure_reason"]
    category = classification.category
    prior_attempts = int(event["retry_count_so_far"]) + attempt_index
    rules: list[str] = []

    # Ensemble signal: note whenever the LLM and the flat heuristic disagree.
    # (Diagnostic only -- it never changes the outcome; the eval harness counts
    #  these and checks which side is right.)
    if not classification.agrees_with_heuristic:
        rules.append(f"note:llm_disagrees_heuristic({classification.heuristic_category})")

    # -- GUARDRAIL 1: hard-decline denylist (independent of the classifier) ---
    if reason in config.HARD_DECLINE_REASONS:
        rules.append("guardrail:hard_decline_denylist")
        # Never retry a denylisted reason. But if the classifier flagged it for
        # review (e.g. a fraud decline on a long clean account -- a possible
        # false positive), send it to a human instead of silently dropping it.
        # Still zero retries either way.
        if category == "needs_review":
            rules.append("route:needs_review")
            return Decision(
                action="escalate_manual_review",
                retry_delay_hours=None,
                scheduled_retry_at=None,
                rationale=(
                    f"'{reason}' is on the hard-decline denylist (never retried), "
                    "but the classifier flagged it as a possible false decline. "
                    "Routing to manual review rather than dropping it."
                ),
                rules_applied=rules,
                guardrail_triggered=True,
            )
        if category != "hard_decline":
            # The LLM got it wrong; the denylist still wins. Record the miss.
            rules.append("note:classifier_disagreed_with_denylist")
        return Decision(
            action="no_action",
            retry_delay_hours=None,
            scheduled_retry_at=None,
            rationale=(
                f"Failure reason '{reason}' is on the hard-decline denylist. "
                "Retrying a blocked, stolen, or fraud-declined instrument is "
                "never permitted, so no recovery action is taken."
            ),
            rules_applied=rules,
            guardrail_triggered=True,
        )

    # -- GUARDRAIL 2: low-confidence escalation (real LLM backends only) ------
    # The offline heuristic's fixed 0.5 is not a calibrated confidence, so this
    # only applies to ollama / claude.
    if (classification.source in ("ollama", "claude")
            and classification.confidence < config.MIN_CONFIDENCE):
        rules.append("guardrail:low_confidence_escalation")
        return Decision(
            action="escalate_manual_review",
            retry_delay_hours=None,
            scheduled_retry_at=None,
            rationale=(
                f"Classifier confidence {classification.confidence:.2f} is below "
                f"the {config.MIN_CONFIDENCE:.2f} threshold. Routing to manual "
                "review rather than acting on a low-confidence label."
            ),
            rules_applied=rules,
            guardrail_triggered=True,
        )

    # -- GUARDRAIL 3: classifier called it a hard decline --------------------
    if category == "hard_decline":
        rules.append("guardrail:classifier_hard_decline")
        return Decision(
            action="no_action",
            retry_delay_hours=None,
            scheduled_retry_at=None,
            rationale=(
                "Classified as a hard decline (issuer refusal that will not "
                "change on retry). No recovery action is taken."
            ),
            rules_applied=rules,
            guardrail_triggered=True,
        )

    # -- GUARDRAIL 4: retry cap --------------------------------------------
    if prior_attempts >= config.MAX_RETRY_ATTEMPTS:
        rules.append("guardrail:retry_cap_reached")
        return Decision(
            action="escalate_manual_review",
            retry_delay_hours=None,
            scheduled_retry_at=None,
            rationale=(
                f"{prior_attempts} recovery attempts already made "
                f"(cap is {config.MAX_RETRY_ATTEMPTS}). Escalating to manual "
                "review rather than retrying further."
            ),
            rules_applied=rules,
            guardrail_triggered=True,
        )

    # -- ROUTING: deterministic category -> action -------------------------
    if category == "needs_reauth":
        rules.append("route:needs_reauth")
        return Decision(
            action="request_mandate_reauth",
            retry_delay_hours=None,
            scheduled_retry_at=None,
            rationale=(
                "Instrument mandate/authorization is no longer valid. Trigger a "
                "mandate re-authorization flow instead of resubmitting the charge."
            ),
            rules_applied=rules,
            guardrail_triggered=False,
        )

    if category == "needs_customer_action":
        rules.append("route:needs_customer_action")
        return Decision(
            action="nudge_customer",
            retry_delay_hours=None,
            scheduled_retry_at=None,
            rationale=(
                "Charge cannot succeed until the customer updates or re-enters "
                "payment details. Send a nudge; do not system-retry."
            ),
            rules_applied=rules,
            guardrail_triggered=False,
        )

    if category == "soft_recoverable":
        delay_hours, scheduled_iso, timing_note = _schedule_retry(event, reason)
        rules.append("route:soft_recoverable")
        rules.append(f"cooldown:{config.COOLDOWN_HOURS}h_floor")
        if reason == "insufficient_funds":
            rules.append("timing:salary_credit_window")
        return Decision(
            action="smart_retry",
            retry_delay_hours=delay_hours,
            scheduled_retry_at=scheduled_iso,
            rationale=timing_note,
            rules_applied=rules,
            guardrail_triggered=False,
        )

    if category == "needs_review":
        rules.append("route:needs_review")
        return Decision(
            action="escalate_manual_review",
            retry_delay_hours=None,
            scheduled_retry_at=None,
            rationale=(
                "Classified as needs_review -- ambiguous, chronic, or a possible "
                "false decline. Routing to a human rather than taking an "
                "automated action."
            ),
            rules_applied=rules,
            guardrail_triggered=False,
        )

    # -- FALLTHROUGH: unrecognized category -> safe escalation --------------
    rules.append("fallthrough:unknown_category")
    return Decision(
        action="escalate_manual_review",
        retry_delay_hours=None,
        scheduled_retry_at=None,
        rationale=(
            f"Classifier returned an unrecognized category ('{category}'). "
            "Routing to a human rather than guessing an action."
        ),
        rules_applied=rules,
        guardrail_triggered=True,
    )


# ---------------------------------------------------------------------------
# Self-demo:  python -m agent.policy_engine
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import os

    from agent.classifier import classify

    here = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(os.path.dirname(here), "data", "failed_payments.json")
    with open(data_path, encoding="utf-8") as fh:
        records = json.load(fh)

    seen: set[str] = set()
    for rec in records:
        if rec["failure_reason"] in seen:
            continue
        seen.add(rec["failure_reason"])
        cls = classify(rec)
        dec = decide(rec, cls)
        print(f"\n{rec['transaction_id']}  {rec['failure_reason']}  "
              f"({rec['transaction_type']}, retries={rec['retry_count_so_far']})")
        print(f"  classify -> {cls.category} [{cls.source}]")
        print(f"  decide   -> {dec.action}"
              + (f" in {dec.retry_delay_hours}h @ {dec.scheduled_retry_at}"
                 if dec.action == "smart_retry" else ""))
        print(f"  rules    -> {dec.rules_applied}")
        print(f"  rationale-> {dec.rationale}")
