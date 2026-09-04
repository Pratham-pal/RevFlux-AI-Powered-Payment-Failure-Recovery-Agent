"""
Synthetic dataset generator for the Payment Failure Recovery Agent.

Produces a batch of realistic *failed* payment events with a deliberately
skewed distribution (insufficient_funds + bank_timeout dominate; hard declines
rare; expired_mandate only on subscriptions).

The important design choice is the ~25% of records that are **context-
dependent**: the raw `failure_reason` points one way, but the surrounding
context (instrument status, repeat-failure history, prior retries, transaction
type) means the correct recovery action is something else. A flat
`failure_reason -> action` lookup gets these wrong; a context-aware classifier
should get them right. That gap is what `eval/run_eval.py` measures.

The five twist families:

  twist_expired_instrument   bank_timeout / network_error, but the card is
                             actually expired -> nudge_customer, not a retry
  twist_chronic_if           insufficient_funds for the 4th+ time in ~10 days
                             -> escalate (needs_review), not another retry
  twist_false_fraud          fraud-suspected decline on a 3-year / 40+ clean
                             -payment account -> never auto-retry a fraud flag,
                             but fast-track human review (needs_review)
  twist_not_transient        a "transient" failure that has already survived
                             two retries on a thin-history account -> it isn't
                             transient; escalate (needs_review)
  twist_reason_type_mismatch expired_mandate on a non-subscription charge ->
                             a data problem; escalate (needs_review)

Two files are written:
  data/failed_payments.json   -> what the agent sees (no labels)
  eval/ground_truth.json      -> hidden labels, keyed by transaction_id:
                                 ground_truth_recoverable / _best_action /
                                 _category / gt_rule / twist
"""

from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.ground_truth import ground_truth_for  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEED = 42
N_RECORDS = 200
REFERENCE_NOW = datetime(2026, 8, 27, 9, 0, 0)

PAYMENT_METHODS = ["card", "upi", "netbanking", "wallet"]
TRANSACTION_TYPES = ["one_time", "subscription", "b2b_invoice"]

MAX_RETRY_ATTEMPTS = 3

# "Clean" records: failure_reason alone determines the right action.
CLEAN_MIX = {
    "insufficient_funds": 48,
    "bank_timeout": 26,
    "network_error": 16,
    "expired_mandate": 16,   # subscription only
    "wrong_cvv": 13,
    "card_expired": 12,
    "invalid_otp": 10,
    "card_blocked": 4,
    "issuer_declined_fraud_suspected": 3,
    "card_reported_stolen": 2,
}

# "Twist" records: context overrides the raw failure_reason. (id, count)
TWIST_MIX = [
    ("twist_expired_instrument", 14),
    ("twist_chronic_if", 12),
    ("twist_false_fraud", 8),
    ("twist_not_transient", 10),
    ("twist_reason_type_mismatch", 6),
]

assert sum(CLEAN_MIX.values()) + sum(c for _, c in TWIST_MIX) == N_RECORDS

HARD_DECLINE = {"card_blocked", "issuer_declined_fraud_suspected", "card_reported_stolen"}
SOFT_REASONS = {"insufficient_funds", "bank_timeout", "network_error"}
TRANSIENT = {"bank_timeout", "network_error"}


# The ground-truth oracle lives in agent/ground_truth.py (shared with the naive
# baseline and the web demo). It is context-aware and priority-ordered.


# ---------------------------------------------------------------------------
# Field builders
# ---------------------------------------------------------------------------

def _random_timestamp(rng: random.Random) -> str:
    return (REFERENCE_NOW - timedelta(seconds=rng.randint(0, 30 * 24 * 3600))).isoformat()


def _amount_for(rng: random.Random, transaction_type: str) -> float:
    if transaction_type == "b2b_invoice":
        return round(rng.uniform(5_000, 250_000), 2)
    if transaction_type == "subscription":
        return round(rng.choice([99, 149, 199, 299, 499, 799, 999, 1499]) * 1.0, 2)
    return round(rng.uniform(100, 15_000), 2)


def _transaction_type(rng: random.Random, reason: str, twist: str | None) -> str:
    if twist == "twist_reason_type_mismatch":
        return rng.choice(["one_time", "b2b_invoice"])       # deliberately NOT subscription
    if reason == "expired_mandate":
        return "subscription"
    return rng.choices(TRANSACTION_TYPES, weights=[0.5, 0.3, 0.2], k=1)[0]


def _payment_method(rng: random.Random, reason: str, twist: str | None) -> str:
    if twist == "twist_expired_instrument":
        return "card"
    if reason == "expired_mandate":
        return "upi"
    if reason in ("wrong_cvv", "card_expired", "card_blocked", "card_reported_stolen"):
        return "card"
    if reason == "invalid_otp":
        return rng.choice(["card", "netbanking"])
    return rng.choice(PAYMENT_METHODS)


def _instrument_status(rng: random.Random, reason: str, twist: str | None) -> str:
    if twist == "twist_expired_instrument":
        return "card_expired"
    if twist == "twist_not_transient":
        return "ok"
    if reason == "card_expired":
        return "card_expired"
    if reason == "card_blocked":
        return "card_blocked"
    if reason == "card_reported_stolen":
        return "card_reported_stolen"
    if reason == "expired_mandate":
        return "mandate_expired"
    return "ok"


def _retry_count(rng: random.Random, reason: str, twist: str | None) -> int:
    if twist == "twist_not_transient":
        return 2
    if reason in HARD_DECLINE:
        return rng.choices([0, 1], weights=[0.9, 0.1], k=1)[0]
    if reason in SOFT_REASONS:
        return rng.choices([0, 1, 2], weights=[0.62, 0.28, 0.10], k=1)[0]
    return rng.choices([0, 1, 2], weights=[0.75, 0.2, 0.05], k=1)[0]


def _customer_history(rng: random.Random, reason: str, twist: str | None) -> dict:
    recent_window = 14

    if twist == "twist_chronic_if":
        return {
            "prior_successful_payments": rng.randint(2, 20),
            "prior_failures": rng.randint(4, 9),
            "account_age_days": rng.randint(120, 1400),
            "recent_failures_same_reason": rng.randint(3, 5),
            "recent_failure_window_days": rng.randint(7, 12),
        }
    if twist == "twist_false_fraud":
        return {
            "prior_successful_payments": rng.randint(42, 95),
            "prior_failures": rng.randint(0, 2),
            "account_age_days": rng.randint(950, 2600),
            "recent_failures_same_reason": 0,
            "recent_failure_window_days": recent_window,
        }
    if twist == "twist_not_transient":
        return {
            "prior_successful_payments": rng.randint(0, 3),
            "prior_failures": rng.randint(1, 4),
            "account_age_days": rng.randint(5, 60),
            "recent_failures_same_reason": rng.randint(1, 2),
            "recent_failure_window_days": rng.randint(3, 10),
        }
    if reason == "issuer_declined_fraud_suspected":
        return {
            "prior_successful_payments": rng.randint(0, 2),
            "prior_failures": rng.randint(0, 3),
            "account_age_days": rng.randint(1, 45),
            "recent_failures_same_reason": rng.randint(0, 1),
            "recent_failure_window_days": recent_window,
        }
    # default
    return {
        "prior_successful_payments": rng.randint(0, 60),
        "prior_failures": rng.randint(0, 6),
        "account_age_days": rng.randint(15, 1800),
        "recent_failures_same_reason": rng.choices([0, 1, 2], weights=[0.72, 0.2, 0.08], k=1)[0],
        "recent_failure_window_days": recent_window,
    }


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

def build_record(i: int, reason: str, twist: str | None, rng: random.Random) -> tuple[dict, dict]:
    transaction_type = _transaction_type(rng, reason, twist)
    record = {
        "transaction_id": f"txn_{i:05d}",
        "timestamp": _random_timestamp(rng),
        "amount": _amount_for(rng, transaction_type),
        "currency": "INR",
        "payment_method": _payment_method(rng, reason, twist),
        "transaction_type": transaction_type,
        "failure_reason": reason,
        "instrument_status": _instrument_status(rng, reason, twist),
        "customer_id": f"cust_{rng.randint(1, 9999):04d}",
        "customer_history": _customer_history(rng, reason, twist),
        "retry_count_so_far": _retry_count(rng, reason, twist),
    }
    return record, ground_truth_for(record)


def build_dataset(rng: random.Random) -> tuple[list[dict], dict]:
    plans: list[tuple[str, str | None]] = []
    for reason, count in CLEAN_MIX.items():
        plans += [(reason, None)] * count
    for twist, count in TWIST_MIX:
        if twist == "twist_expired_instrument":
            reasons = ["bank_timeout", "network_error"]
        elif twist == "twist_chronic_if":
            reasons = ["insufficient_funds"]
        elif twist == "twist_false_fraud":
            reasons = ["issuer_declined_fraud_suspected"]
        elif twist == "twist_not_transient":
            reasons = ["bank_timeout", "network_error"]
        else:  # twist_reason_type_mismatch
            reasons = ["expired_mandate"]
        for k in range(count):
            plans.append((reasons[k % len(reasons)], twist))

    rng.shuffle(plans)

    records: list[dict] = []
    ground_truth: dict[str, dict] = {}
    for i, (reason, twist) in enumerate(plans, start=1):
        rec, gt = build_record(i, reason, twist, rng)
        records.append(rec)
        ground_truth[rec["transaction_id"]] = gt
    return records, ground_truth


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    rng = random.Random(SEED)
    records, ground_truth = build_dataset(rng)

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    data_dir = os.path.join(root, "data")
    eval_dir = os.path.join(root, "eval")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(eval_dir, exist_ok=True)

    data_path = os.path.join(data_dir, "failed_payments.json")
    gt_path = os.path.join(eval_dir, "ground_truth.json")
    with open(data_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
    with open(gt_path, "w", encoding="utf-8") as fh:
        json.dump(ground_truth, fh, indent=2)

    print(f"Wrote {len(records)} records -> {data_path}")
    print(f"Wrote {len(ground_truth)} ground-truth labels -> {gt_path}\n")

    from collections import Counter
    by_reason = Counter(r["failure_reason"] for r in records)
    by_cat = Counter(g["ground_truth_category"] for g in ground_truth.values())
    by_rule = Counter(g["gt_rule"] for g in ground_truth.values())
    twist_n = sum(1 for g in ground_truth.values() if g["twist"])
    recov_n = sum(1 for g in ground_truth.values() if g["ground_truth_recoverable"])

    print("failure_reason:")
    for k, v in by_reason.most_common():
        print(f"  {k:32s} {v:4d}")
    print("\nground_truth_category:")
    for k, v in by_cat.most_common():
        print(f"  {k:24s} {v:4d}")
    print("\ngt_rule:")
    for k, v in by_rule.most_common():
        print(f"  {k:28s} {v:4d}")
    print(f"\ncontext-dependent (twist) records : {twist_n} / {len(records)} "
          f"({100*twist_n/len(records):.0f}%)")
    print(f"ground_truth_recoverable == True  : {recov_n} / {len(records)}")


if __name__ == "__main__":
    main()
