"""The baseline we're competing against: a dumb "retry everything" strategy.

This is roughly what a lot of payment recovery looks like in practice -- a cron
job that re-attempts every failed charge a few times, with no idea why it
failed. No classification, no cooldown, no hard-decline protection, no smart
timing.

Used by the web demo to show, side by side, what the agent avoids:
  * retries burned on blocked / stolen / fraud-declined cards (never recover,
    cost money, and push more declines at the issuer)
  * retries burned on expired mandates and wrong-CVV failures (a resubmit of the
    same charge can't fix those)
  * back-to-back retries with no cooldown

For a genuinely transient failure the naive strategy does about as well as the
agent -- which is the point: the agent's edge is entirely in *not* wasting
effort on the failures a retry can't fix.
"""

from __future__ import annotations

import random

from agent import config
from agent.ground_truth import ground_truth_for


def run_naive_recovery(event: dict, *, rng: random.Random) -> dict:
    """Retry the charge up to MAX_RETRY_ATTEMPTS times, immediately, no matter what."""
    reason = event["failure_reason"]
    # The failure's *true* nature (context-aware), so the simulated outcome is
    # honest -- a naive retry of an expired-card "timeout" really does fail.
    true_category = ground_truth_for(event)["ground_truth_category"]
    if true_category == "needs_review":
        true_category = "hard_decline"  # for the sim: a retry won't resolve it
    amount = float(event["amount"])

    attempts = 0
    recovered = False
    for _ in range(config.MAX_RETRY_ATTEMPTS):
        attempts += 1
        p = config.INTERVENTION_EFFECTIVENESS["smart_retry"].get(true_category, 0.0)
        if rng.random() < p:
            recovered = True
            break

    # Every retry against a non-transient failure is wasted spend. Against a
    # transient failure, only the attempts before a success (if any) count.
    if true_category == "soft_recoverable":
        wasted_retries = 0 if recovered else attempts
    else:
        wasted_retries = attempts

    return {
        "strategy": "naive_retry_all",
        "attempts": attempts,
        "recovered": recovered,
        "final_outcome": "recovered" if recovered else "still_failed",
        "amount_recovered": round(amount if recovered else 0.0, 2),
        "wasted_retries": wasted_retries,
        "wasted_cost_inr": round(wasted_retries * config.WASTED_RETRY_COST_INR, 2),
        "cooldown_respected": False,
        "hard_decline_protection": False,
        "extra_issuer_declines": attempts if true_category == "hard_decline" else 0,
    }
