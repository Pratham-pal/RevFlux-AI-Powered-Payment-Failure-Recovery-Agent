"""Dry-run the classifier + policy engine over the whole dataset.

No executor, no simulation, no audit log -- this just shows what category each
failure gets and what first action the policy engine would take, so the
classify/decide layer can be inspected before the rest of the pipeline is built.

    python scripts/preview_decisions.py
    RECOVERY_AGENT_OFFLINE=1 python scripts/preview_decisions.py   # force heuristic
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.classifier import classify
from agent.policy_engine import decide

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    with open(os.path.join(ROOT, "data", "failed_payments.json"), encoding="utf-8") as fh:
        records = json.load(fh)
    with open(os.path.join(ROOT, "eval", "ground_truth.json"), encoding="utf-8") as fh:
        ground_truth = json.load(fh)

    cat_by_reason: dict[str, Counter] = {}
    action_by_reason: dict[str, Counter] = {}
    source_counter: Counter = Counter()
    guardrail_hits = 0
    action_vs_gt = Counter()  # (agent_action, ground_truth_best_action)

    for rec in records:
        cls = classify(rec)
        dec = decide(rec, cls)
        reason = rec["failure_reason"]

        cat_by_reason.setdefault(reason, Counter())[cls.category] += 1
        action_by_reason.setdefault(reason, Counter())[dec.action] += 1
        source_counter[cls.source] += 1
        guardrail_hits += int(dec.guardrail_triggered)

        gt = ground_truth[rec["transaction_id"]]["ground_truth_best_action"]
        action_vs_gt[(dec.action, gt)] += 1

    print(f"records: {len(records)}   classifier source: {dict(source_counter)}   "
          f"guardrail-forced decisions: {guardrail_hits}\n")

    print("failure_reason           ->  category (count)            |  first action (count)")
    print("-" * 92)
    for reason in sorted(cat_by_reason):
        cats = ", ".join(f"{k}:{v}" for k, v in cat_by_reason[reason].most_common())
        acts = ", ".join(f"{k}:{v}" for k, v in action_by_reason[reason].most_common())
        print(f"{reason:24s} ->  {cats:34s} |  {acts}")

    print("\nfirst action  vs  ground_truth_best_action")
    print("-" * 60)
    agree = sum(v for (a, g), v in action_vs_gt.items() if a == g)
    for (a, g), v in sorted(action_vs_gt.items(), key=lambda kv: -kv[1]):
        flag = "" if a == g else "  <- differs"
        print(f"  agent={a:24s} gt={g:24s} {v:4d}{flag}")
    print(f"\n  exact action match: {agree}/{len(records)} ({100*agree/len(records):.1f}%)")
    print("  (note: needs_customer_action legitimately maps to nudge_customer even")
    print("   when gt best_action is 'nudge_customer'; escalation cases differ by design)")


if __name__ == "__main__":
    main()
