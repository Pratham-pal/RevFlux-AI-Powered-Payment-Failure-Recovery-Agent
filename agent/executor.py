"""Stage 3: the executor (simulated).

NO real payment calls are ever made. The executor takes the policy engine's
decision and simulates the outcome. Success probability depends on the action
AND the failure category (config.INTERVENTION_EFFECTIVENESS); because the agent
always pairs the fitting action with the category, in practice this is:

    smart_retry  on soft_recoverable      -> succeeds ~60% of the time
    request_mandate_reauth on needs_reauth -> succeeds ~70% of the time
    nudge_customer on needs_customer_action -> succeeds ~40% of the time
    no_action / escalate                   -> never auto-resolve

It also owns the retry *loop*: for a `smart_retry` that fails, it calls the
policy engine again with an incremented attempt index. The policy engine (not
the executor) decides when to stop -- once the lifetime cap is reached it
returns `escalate_manual_review` and the loop ends. The loop is therefore
bounded by config.MAX_RETRY_ATTEMPTS by construction; it cannot run away.

Outcome vocabulary (per transaction, final):
    recovered         - a simulated action succeeded
    still_failed      - a customer nudge went out but did not convert
    escalated         - retries exhausted, or mandate re-auth not completed
    no_action_taken   - hard decline; correctly left alone
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

from agent import config
from agent.audit_log import AuditLogger
from agent.classifier import Classification, classify
from agent.policy_engine import Decision, decide

RETRYABLE_ACTIONS = ("smart_retry", "request_mandate_reauth", "nudge_customer")


@dataclass
class RecoveryResult:
    """Everything the batch runner needs to know about one transaction."""

    transaction_id: str
    failure_reason: str
    payment_method: str
    transaction_type: str
    amount: float

    category: str
    classification_source: str
    classification_confidence: float
    classification_reasoning: str
    heuristic_category: str
    agrees_with_heuristic: bool

    first_action: str          # the action chosen on attempt 1
    final_action: str          # the last action taken
    final_outcome: str          # recovered | still_failed | escalated | no_action_taken

    total_attempts: int         # actionable attempts made this run
    retry_attempts: int         # of those, how many were smart_retry executions
    retried: bool               # did we execute at least one smart_retry
    guardrail_triggered: bool    # did any hard guardrail fire
    escalated: bool

    amount_recovered: float
    rules_fired: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _simulate_success(action: str, category: str, rng: random.Random) -> bool:
    """Simulated outcome of a single action. Deterministic given `rng`.

    Uses INTERVENTION_EFFECTIVENESS[action][category]; unknown pairs (e.g.
    no_action / escalate_manual_review) resolve to 0.0 -> never auto-resolve.
    """
    rate = config.INTERVENTION_EFFECTIVENESS.get(action, {}).get(category, 0.0)
    return rng.random() < rate


def run_recovery(
    event: dict,
    *,
    rng: random.Random,
    logger: AuditLogger | None = None,
    classification: Classification | None = None,
) -> RecoveryResult:
    """Run the full classify -> decide -> execute loop for one failed payment.

    Pass `classification` to reuse an already-computed result (avoids a second
    LLM call when the caller has already classified the event).
    """
    cls: Classification = classification or classify(event)

    amount = float(event["amount"])
    txn_id = event["transaction_id"]
    reason = event["failure_reason"]

    attempts = 0          # actionable attempts made this run
    retry_attempts = 0
    rules_fired: list[str] = []
    guardrail_any = False
    first_action: str | None = None

    def _record(attempt_number: int, decision: Decision, outcome: str, recovered_amt: float):
        nonlocal guardrail_any
        guardrail_any = guardrail_any or decision.guardrail_triggered
        for r in decision.rules_applied:
            if r not in rules_fired:
                rules_fired.append(r)
        if logger is not None:
            logger.log(
                transaction_id=txn_id,
                failure_reason=reason,
                amount=amount,
                attempt_number=attempt_number,
                classification=cls,
                decision=decision,
                outcome=outcome,
                simulated_amount_recovered=recovered_amt,
            )

    while True:
        decision = decide(event, cls, attempt_index=attempts)
        if first_action is None:
            first_action = decision.action

        # --- terminal, non-actionable decisions -------------------------
        if decision.action == "no_action":
            _record(attempts + 1, decision, "no_action_taken", 0.0)
            return _result(event, cls, first_action, "no_action", "no_action_taken",
                           attempts, retry_attempts, guardrail_any, False, 0.0, rules_fired)

        if decision.action == "escalate_manual_review":
            _record(attempts + 1, decision, "escalated", 0.0)
            return _result(event, cls, first_action, "escalate_manual_review", "escalated",
                           attempts, retry_attempts, guardrail_any, True, 0.0, rules_fired)

        # --- actionable: simulate it -----------------------------------
        attempts += 1
        if decision.action == "smart_retry":
            retry_attempts += 1

        if _simulate_success(decision.action, cls.category, rng):
            _record(attempts, decision, "recovered", amount)
            return _result(event, cls, first_action, decision.action, "recovered",
                           attempts, retry_attempts, guardrail_any, False, amount, rules_fired)

        # action failed this attempt
        _record(attempts, decision, "still_failed", 0.0)

        if decision.action == "smart_retry":
            # Loop. The policy engine escalates once the cap is hit.
            continue

        if decision.action == "request_mandate_reauth":
            # A single re-auth attempt; if the customer doesn't complete it,
            # route to a human rather than resubmitting the charge.
            esc = Decision(
                action="escalate_manual_review",
                retry_delay_hours=None,
                scheduled_retry_at=None,
                rationale="Mandate re-authorization was not completed; routing to manual review.",
                rules_applied=["route:needs_reauth", "followup:reauth_not_completed"],
                guardrail_triggered=True,
            )
            _record(attempts + 1, esc, "escalated", 0.0)
            return _result(event, cls, first_action, "escalate_manual_review", "escalated",
                           attempts, retry_attempts, guardrail_any, True, 0.0, rules_fired)

        # nudge_customer that didn't convert: a soft miss, awaiting the customer.
        return _result(event, cls, first_action, "nudge_customer", "still_failed",
                       attempts, retry_attempts, guardrail_any, False, 0.0, rules_fired)


def _result(event, cls, first_action, final_action, final_outcome,
            total_attempts, retry_attempts, guardrail, escalated, recovered_amt,
            rules_fired) -> RecoveryResult:
    return RecoveryResult(
        transaction_id=event["transaction_id"],
        failure_reason=event["failure_reason"],
        payment_method=event["payment_method"],
        transaction_type=event["transaction_type"],
        amount=float(event["amount"]),
        category=cls.category,
        classification_source=cls.source,
        classification_confidence=cls.confidence,
        classification_reasoning=cls.reasoning,
        heuristic_category=cls.heuristic_category,
        agrees_with_heuristic=cls.agrees_with_heuristic,
        first_action=first_action,
        final_action=final_action,
        final_outcome=final_outcome,
        total_attempts=total_attempts,
        retry_attempts=retry_attempts,
        retried=retry_attempts > 0,
        guardrail_triggered=guardrail,
        escalated=escalated,
        amount_recovered=round(recovered_amt, 2),
        rules_fired=list(rules_fired),
    )
