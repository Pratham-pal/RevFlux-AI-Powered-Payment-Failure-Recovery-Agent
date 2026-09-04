"""The ground-truth policy: given a full failed-payment record, what is the
correct recovery category and action?

This is the *oracle* the classifier is scored against. It is deliberately
context-aware -- the rules are checked in priority order, context before the
flat `failure_reason` default -- so a record whose raw failure code points one
way but whose context points another resolves to the context answer.

Used by:
  * data/generate_dataset.py  -> to label the synthetic dataset
  * agent/naive.py            -> to know a failure's true nature for the sim
  * web/recovery_service.py   -> to show "the ideal answer" alongside the agent's

The agent's classifier NEVER imports this -- it has to earn the answer.
"""

from __future__ import annotations

from agent import config

_HARD = config.HARD_DECLINE_REASONS
_SOFT = config.SOFT_RECOVERABLE_REASONS
_TRANSIENT = config.TRANSIENT_REASONS
_MAX_RETRIES = config.MAX_RETRY_ATTEMPTS


def ground_truth_for(rec: dict) -> dict:
    """Return {ground_truth_recoverable, ground_truth_best_action,
    ground_truth_category, gt_rule, twist} for one record."""
    reason = rec["failure_reason"]
    hist = rec.get("customer_history", {})
    prior = int(rec.get("retry_count_so_far", 0))
    inst = rec.get("instrument_status", "ok")
    ttype = rec.get("transaction_type", "one_time")

    prior_ok = hist.get("prior_successful_payments", 0)
    account_age = hist.get("account_age_days", 0)
    recent_same = hist.get("recent_failures_same_reason", 0)

    def out(recoverable, action, category, rule):
        return {
            "ground_truth_recoverable": recoverable,
            "ground_truth_best_action": action,
            "ground_truth_category": category,
            "gt_rule": rule,
            "twist": rule if rule.startswith("twist_") else None,
        }

    # -- context rules (checked before the flat base rules) -----------
    if reason == "issuer_declined_fraud_suspected" and prior_ok >= 40 and account_age >= 900:
        # A fraud flag on a long, clean account is likely a false positive.
        # NEVER auto-retry a fraud decline -- a strong history only earns it a
        # fast human review instead of a silent loss.
        return out(False, "escalate_manual_review", "needs_review", "twist_false_fraud")

    if reason in _HARD:
        return out(False, "no_action", "hard_decline", "base_hard_decline")

    if reason == "expired_mandate" and ttype != "subscription":
        return out(False, "escalate_manual_review", "needs_review", "twist_reason_type_mismatch")

    if reason in _TRANSIENT and inst == "card_expired":
        return out(False, "nudge_customer", "needs_customer_action", "twist_expired_instrument")

    if reason == "insufficient_funds" and recent_same >= 3:
        return out(False, "escalate_manual_review", "needs_review", "twist_chronic_if")

    if reason in _TRANSIENT and prior >= 2 and prior_ok <= 3:
        return out(False, "escalate_manual_review", "needs_review", "twist_not_transient")

    if reason in _SOFT and prior >= _MAX_RETRIES:
        return out(False, "escalate_manual_review", "needs_review", "base_retry_exhausted")

    # -- flat base rules ---------------------------------------------
    if reason in _SOFT:
        return out(True, "smart_retry", "soft_recoverable", "base_soft_recoverable")
    if reason == "expired_mandate":
        return out(False, "request_mandate_reauth", "needs_reauth", "base_needs_reauth")
    return out(False, "nudge_customer", "needs_customer_action", "base_needs_customer_action")
