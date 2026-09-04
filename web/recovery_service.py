"""Bridges the `agent/` pipeline to the web layer.

Builds a failed-payment event from a demo scenario, runs it through the real
classifier + policy engine + executor, runs the naive baseline for comparison,
and packages everything the front-end needs into one JSON-serializable dict.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime

from agent import config
from agent.classifier import Classification, active_backend, classify
from agent.executor import run_recovery
from agent.ground_truth import ground_truth_for
from agent.naive import run_naive_recovery
from agent.policy_engine import decide

# ---------------------------------------------------------------------------
# Demo scenarios  (the failure-scenario dropdown)
# ---------------------------------------------------------------------------
# `overrides` is merged into the generated event -- this is how the "twist"
# scenarios inject the context that makes failure_reason alone misleading.

SCENARIOS: dict[str, dict] = {
    "insufficient_funds": {
        "label": "Insufficient funds",
        "blurb": "One-off balance shortfall on a healthy account.",
        "failure_reason": "insufficient_funds",
        "payment_method": "card",
        "transaction_type": "one_time",
        "expected_category": "soft_recoverable",
    },
    "bank_timeout": {
        "label": "Bank / network timeout",
        "blurb": "The issuing bank didn't respond in time. Usually transient.",
        "failure_reason": "bank_timeout",
        "payment_method": "netbanking",
        "transaction_type": "one_time",
        "expected_category": "soft_recoverable",
    },
    "expired_mandate": {
        "label": "Expired UPI AutoPay mandate (subscription)",
        "blurb": "The recurring-payment mandate has lapsed.",
        "failure_reason": "expired_mandate",
        "payment_method": "upi",
        "transaction_type": "subscription",
        "expected_category": "needs_reauth",
    },
    "wrong_cvv": {
        "label": "Wrong CVV",
        "blurb": "Card details entered incorrectly. A resubmit can't fix this.",
        "failure_reason": "wrong_cvv",
        "payment_method": "card",
        "transaction_type": "one_time",
        "expected_category": "needs_customer_action",
    },
    "card_blocked": {
        "label": "Card blocked by issuer",
        "blurb": "The issuing bank blocked the card. Retrying is pointless and abusive.",
        "failure_reason": "card_blocked",
        "payment_method": "card",
        "transaction_type": "one_time",
        "expected_category": "hard_decline",
    },
    "fraud_thin": {
        "label": "Fraud-suspected decline — new account",
        "blurb": "Fraud flag on a 3-week-old account with no history. A real hard decline.",
        "failure_reason": "issuer_declined_fraud_suspected",
        "payment_method": "card",
        "transaction_type": "one_time",
        "expected_category": "hard_decline",
        "overrides": {"customer_history": {
            "prior_successful_payments": 0, "prior_failures": 2,
            "account_age_days": 21, "recent_failures_same_reason": 0,
            "recent_failure_window_days": 14}},
    },
    # ---- twist scenarios: failure_reason alone gives the wrong answer -----
    "twist_expired_card": {
        "label": "★ Timeout — but the card is expired",
        "blurb": "Failure code says 'bank_timeout', but the instrument is expired. "
                 "A retry can't succeed; the customer must update the card.",
        "failure_reason": "bank_timeout",
        "payment_method": "card",
        "transaction_type": "one_time",
        "expected_category": "needs_customer_action",
        "overrides": {"instrument_status": "card_expired"},
    },
    "twist_chronic_if": {
        "label": "★ Insufficient funds — 4th time in 10 days",
        "blurb": "Chronic shortfall, not a timing blip. Retrying again won't help — "
                 "a human should decide whether to pause billing.",
        "failure_reason": "insufficient_funds",
        "payment_method": "upi",
        "transaction_type": "subscription",
        "expected_category": "needs_review",
        "overrides": {"customer_history": {
            "prior_successful_payments": 6, "prior_failures": 7,
            "account_age_days": 300, "recent_failures_same_reason": 4,
            "recent_failure_window_days": 10}},
    },
    "twist_false_fraud": {
        "label": "★ Fraud decline — on a 4-year loyal customer",
        "blurb": "Fraud flag on a long, clean account — likely a false positive. "
                 "Never auto-retry a fraud flag, but fast-track human review.",
        "failure_reason": "issuer_declined_fraud_suspected",
        "payment_method": "card",
        "transaction_type": "one_time",
        "expected_category": "needs_review",
        "overrides": {"customer_history": {
            "prior_successful_payments": 63, "prior_failures": 1,
            "account_age_days": 1450, "recent_failures_same_reason": 0,
            "recent_failure_window_days": 14}},
    },
}

_TWIST_IDS = {"twist_expired_card", "twist_chronic_if", "twist_false_fraud"}


def scenario_list() -> list[dict]:
    return [{"id": k, "twist": k in _TWIST_IDS,
             **{kk: v[kk] for kk in ("label", "blurb", "expected_category")}}
            for k, v in SCENARIOS.items()]


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------

def _synth_history(failure_reason: str, rng: random.Random) -> dict:
    if failure_reason == "issuer_declined_fraud_suspected":
        base = {
            "prior_successful_payments": rng.randint(0, 2),
            "prior_failures": rng.randint(0, 3),
            "account_age_days": rng.randint(1, 40),
        }
    else:
        base = {
            "prior_successful_payments": rng.randint(3, 55),
            "prior_failures": rng.randint(0, 5),
            "account_age_days": rng.randint(60, 1500),
        }
    base["recent_failures_same_reason"] = rng.choice([0, 0, 0, 1])
    base["recent_failure_window_days"] = 14
    return base


def build_event(
    scenario_id: str,
    amount: float,
    retry_count_so_far: int = 0,
    razorpay_error: dict | None = None,
) -> dict:
    if scenario_id not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario_id!r}")
    s = SCENARIOS[scenario_id]
    rng = random.Random()

    event = {
        "transaction_id": "live_" + uuid.uuid4().hex[:8],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "amount": round(float(amount), 2),
        "currency": "INR",
        "payment_method": s["payment_method"],
        "transaction_type": s["transaction_type"],
        "failure_reason": s["failure_reason"],
        "instrument_status": "ok",
        "customer_id": f"cust_{rng.randint(1000, 9999)}",
        "customer_history": _synth_history(s["failure_reason"], rng),
        "retry_count_so_far": int(retry_count_so_far),
        "_razorpay_error": razorpay_error,
    }
    # instrument status implied by the reason
    if s["failure_reason"] == "card_blocked":
        event["instrument_status"] = "card_blocked"
    elif s["failure_reason"] == "expired_mandate":
        event["instrument_status"] = "mandate_expired"

    for key, val in s.get("overrides", {}).items():
        if key == "customer_history":
            event["customer_history"].update(val)
        else:
            event[key] = val
    return event


# ---------------------------------------------------------------------------
# Guardrail trace  (drives the policy-engine animation; mirrors decide())
# ---------------------------------------------------------------------------

def guardrail_trace(event: dict, cls: Classification, decision) -> list[dict]:
    reason = event["failure_reason"]
    prior = int(event["retry_count_so_far"])
    on_denylist = reason in config.HARD_DECLINE_REASONS
    cap_hit = prior >= config.MAX_RETRY_ATTEMPTS
    low_conf = (cls.source in ("ollama", "claude")
               and cls.confidence < config.MIN_CONFIDENCE)

    checks: list[dict] = [{
        "key": "disagreement",
        "label": "LLM vs. flat heuristic",
        "status": "info",
        "detail": (
            f"agree — both say {cls.category}" if cls.agrees_with_heuristic
            else f"disagree — LLM: {cls.category}, flat lookup: {cls.heuristic_category}"
        ),
    }, {
        "key": "hard_decline_denylist",
        "label": "Hard-decline denylist",
        "status": "block" if on_denylist else "pass",
        "detail": (
            f"'{reason}' is on the never-retry denylist — retry blocked in "
            "deterministic code, whatever the classifier said"
            if on_denylist else
            f"'{reason}' is not a blocked / stolen / fraud-declined instrument"
        ),
    }, {
        "key": "confidence_gate",
        "label": f"Confidence ≥ {config.MIN_CONFIDENCE:.2f}",
        "status": "block" if low_conf else "pass",
        "detail": (
            f"confidence {cls.confidence:.2f} below threshold — escalate to a human"
            if low_conf else
            (f"confidence {cls.confidence:.2f}" if cls.source in ("ollama", "claude")
             else "n/a — offline heuristic has no calibrated confidence")
        ),
    }, {
        "key": "classifier_verdict",
        "label": "Classifier verdict",
        "status": "block" if (cls.category == "hard_decline" and not on_denylist) else "pass",
        "detail": (
            "classifier returned hard_decline — no recovery action"
            if cls.category == "hard_decline" else f"classifier returned {cls.category}"
        ),
    }, {
        "key": "retry_cap",
        "label": f"Retry cap ({prior}/{config.MAX_RETRY_ATTEMPTS})",
        "status": "block" if cap_hit else "pass",
        "detail": (
            f"{prior} attempts already used — escalate to manual review" if cap_hit
            else f"{config.MAX_RETRY_ATTEMPTS - prior} attempt(s) remaining"
        ),
    }]
    if decision.action == "smart_retry":
        checks.append({
            "key": "cooldown",
            "label": f"Cooldown ≥ {config.COOLDOWN_HOURS}h",
            "status": "pass",
            "detail": (f"next attempt scheduled {decision.retry_delay_hours}h out "
                       f"({decision.scheduled_retry_at})"),
        })
    checks.append({
        "key": "route",
        "label": "Route",
        "status": "info",
        "detail": f"{cls.category} → {decision.action}",
    })
    return checks


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------

_IDEAL_ACTION = {
    "hard_decline": "no_action",
    "soft_recoverable": "smart_retry",
    "needs_reauth": "request_mandate_reauth",
    "needs_customer_action": "nudge_customer",
    "needs_review": "escalate_manual_review",
}


def _ideal_action(true_category: str) -> str:
    return _IDEAL_ACTION.get(true_category, "escalate_manual_review")

def _narrative(true_category: str, naive: dict, agent, decision, gt_rule: str,
               classifier_correct: bool) -> str:
    n = naive["attempts"]
    ao = agent.final_outcome

    if not classifier_correct:
        return (
            f"On this run the classifier mislabelled the case — the offline "
            f"heuristic can't see the context that matters here. Switch the "
            f"backend to Ollama or Claude (top-right badge) and it routes "
            f"correctly to '{true_category}'. The ideal action is "
            f"'{_ideal_action(true_category)}'."
        )

    if true_category == "hard_decline":
        return (
            f"Naive logic burned {n} retries on an instrument the issuer had "
            f"already refused — still failed, and pushed {n} more declines at the "
            f"issuer (which hurts future auth rates). The agent recognised the "
            f"hard decline and stopped immediately: ₹{naive['wasted_cost_inr']:.0f} "
            f"saved and the issuer relationship protected."
        )
    if true_category == "needs_review":
        why = {
            "twist_chronic_if": "four insufficient-funds failures in ten days is a chronic "
                                "shortfall, not a timing blip",
            "twist_false_fraud": "a fraud flag on a 4-year account with 60+ clean payments "
                                 "is very likely a false positive",
            "twist_not_transient": "a 'transient' error that already survived two retries "
                                   "isn't transient",
            "twist_reason_type_mismatch": "an expired-mandate code on a one-off charge is a "
                                          "data problem",
        }.get(gt_rule, "the situation is ambiguous")
        return (
            f"Naive logic retried {n}x and failed — {why}. The agent caught this "
            f"from the context and routed it to human review instead of wasting "
            f"retries or silently losing the payment."
        )
    if true_category == "needs_reauth":
        return (
            f"A blind retry can't fix an expired mandate — naive retried {n}x and "
            f"failed. The agent triggered a mandate re-authorization flow instead ({ao})."
        )
    if true_category == "needs_customer_action":
        extra = (" The failure code said 'timeout', but the real problem was an "
                 "expired card — the agent saw that from the instrument status."
                 if gt_rule == "twist_expired_instrument" else "")
        return (
            f"Resubmitting the same charge can't produce a valid card / CVV / OTP — "
            f"naive retried {n}x and failed. The agent nudged the customer to update "
            f"their details instead ({ao}).{extra}"
        )
    # soft_recoverable
    salary_timed = "timing:salary_credit_window" in (decision.rules_applied or [])
    timing = "next salary-credit window" if salary_timed else "cooldown window"
    return (
        f"Both strategies can recover this one. The agent still adds value: it "
        f"scheduled the retry for the {timing} (in {decision.retry_delay_hours}h) "
        f"instead of hammering the bank immediately, and respected the "
        f"{config.COOLDOWN_HOURS}h cooldown."
    )


# ---------------------------------------------------------------------------
# The demo run
# ---------------------------------------------------------------------------

def run_demo(
    scenario_id: str,
    amount: float,
    retry_count_so_far: int = 0,
    razorpay_error: dict | None = None,
) -> dict:
    event = build_event(scenario_id, amount, retry_count_so_far, razorpay_error)
    ideal = ground_truth_for(event)
    true_category = ideal["ground_truth_category"]

    cls = classify(event)
    decision = decide(event, cls)
    trace = guardrail_trace(event, cls, decision)

    agent = run_recovery(event, rng=random.Random(), classification=cls)
    naive = run_naive_recovery(event, rng=random.Random())

    agent_wasted_retries = agent.retry_attempts if true_category != "soft_recoverable" else 0
    agent_wasted_cost = round(agent_wasted_retries * config.WASTED_RETRY_COST_INR, 2)
    savings_inr = round(naive["wasted_cost_inr"] - agent_wasted_cost, 2)

    public_event = {k: v for k, v in event.items() if not k.startswith("_")}

    return {
        "event": public_event,
        "razorpay_error": razorpay_error,
        "classifier_backend": active_backend(),
        "classification": cls.to_dict(),
        "decision": decision.to_dict(),
        "guardrail_trace": trace,
        "agent_run": agent.to_dict(),
        "naive_run": naive,
        "ideal": {
            "category": ideal["ground_truth_category"],
            "best_action": ideal["ground_truth_best_action"],
            "classifier_correct": cls.category == ideal["ground_truth_category"],
        },
        "comparison": {
            "true_category": true_category,
            "gt_rule": ideal["gt_rule"],
            "savings_inr": savings_inr,
            "agent_recovered": agent.final_outcome == "recovered",
            "naive_recovered": naive["recovered"],
            "agent_attempts": agent.total_attempts,
            "naive_attempts": naive["attempts"],
            "agent_wasted_retries": agent_wasted_retries,
            "naive_wasted_retries": naive["wasted_retries"],
            "extra_issuer_declines_avoided": naive["extra_issuer_declines"],
            "narrative": _narrative(true_category, naive, agent, decision,
                                    ideal["gt_rule"],
                                    cls.category == ideal["ground_truth_category"]),
        },
    }
