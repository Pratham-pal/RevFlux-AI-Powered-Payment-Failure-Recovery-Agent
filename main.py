"""Batch runner: run the full recovery pipeline over the synthetic dataset and
score it against the hidden ground truth.

    python main.py

Outputs:
    results/audit_log.jsonl   - every decision, one JSON line each
    results/summary.json      - machine-readable metrics
    results/report.md         - human-readable report

The scoring uses eval/ground_truth.json, which the agent never sees. It reports
two separate accuracy views, on purpose:

  * decision-to-retry precision/recall - did the agent retry the right things?
    (ground_truth_recoverable is the label). This is where a wrongly-retried
    hard decline or dead card shows up as a false positive.
  * best-action agreement - did the agent pick the right *kind* of intervention
    overall, including the reauth and nudge paths that are legitimate
    recoveries but not blind retries (ground_truth_best_action is the label).
"""

from __future__ import annotations

import json
import os
import random
from collections import Counter, defaultdict
from datetime import datetime

from agent import config
from agent.audit_log import AuditLogger
from agent.classifier import active_backend
from agent.executor import RecoveryResult, run_recovery

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(ROOT, "data", "failed_payments.json")
GROUND_TRUTH_PATH = os.path.join(ROOT, "eval", "ground_truth.json")
RESULTS_DIR = os.path.join(ROOT, "results")

# Fixed seed so the simulated outcomes (and therefore the report) are
# reproducible for a given classification pass.
SIMULATION_SEED = 7


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _rate(num: int, denom: int) -> float:
    return round(num / denom, 4) if denom else 0.0


def score(results: list[RecoveryResult], ground_truth: dict) -> dict:
    total = len(results)
    recovered = [r for r in results if r.final_outcome == "recovered"]

    # -- recovery rate by failure_reason --------------------------------
    by_reason: dict[str, dict] = {}
    reason_groups: dict[str, list] = defaultdict(list)
    for r in results:
        reason_groups[r.failure_reason].append(r)
    for reason, group in sorted(reason_groups.items()):
        rec = [r for r in group if r.final_outcome == "recovered"]
        by_reason[reason] = {
            "total": len(group),
            "recovered": len(rec),
            "recovery_rate": _rate(len(rec), len(group)),
            "amount_at_risk": round(sum(r.amount for r in group), 2),
            "amount_recovered": round(sum(r.amount_recovered for r in group), 2),
        }

    # -- recovery rate by (LLM) category -------------------------------
    by_category: dict[str, dict] = {}
    cat_groups: dict[str, list] = defaultdict(list)
    for r in results:
        cat_groups[r.category].append(r)
    for cat, group in sorted(cat_groups.items()):
        rec = [r for r in group if r.final_outcome == "recovered"]
        by_category[cat] = {
            "total": len(group),
            "recovered": len(rec),
            "recovery_rate": _rate(len(rec), len(group)),
        }

    # -- hard-decline restraint ---------------------------------------
    gt_leave_alone = [r for r in results
                      if ground_truth[r.transaction_id]["ground_truth_best_action"] == "no_action"]
    correctly_left_alone = [r for r in gt_leave_alone
                            if r.final_action == "no_action" and not r.retried]
    hard_declines_retried = [r for r in gt_leave_alone if r.retried]

    # -- false-positive cost: retries spent on non-recoverable failures --
    wasted = [r for r in results
              if r.retried and not ground_truth[r.transaction_id]["ground_truth_recoverable"]]
    wasted_retry_attempts = sum(r.retry_attempts for r in wasted)

    # -- decision-to-retry precision / recall -------------------------
    tp = fp = fn = tn = 0
    for r in results:
        predicted_retry = r.first_action == "smart_retry"
        actual_retry = ground_truth[r.transaction_id]["ground_truth_recoverable"]
        if predicted_retry and actual_retry:
            tp += 1
        elif predicted_retry and not actual_retry:
            fp += 1
        elif not predicted_retry and actual_retry:
            fn += 1
        else:
            tn += 1
    precision = _rate(tp, tp + fp)
    recall = _rate(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 4) if (precision + recall) else 0.0

    # -- best-action agreement --------------------------------------
    best_action_match = sum(
        1 for r in results
        if r.first_action == ground_truth[r.transaction_id]["ground_truth_best_action"]
    )

    # -- classifier quality: LLM vs the flat heuristic --------------
    cat_llm = cat_heur = 0
    cat_llm_twist = cat_heur_twist = n_twist = 0
    cat_llm_clean = cat_heur_clean = n_clean = 0
    disagreements = 0
    for r in results:
        gt = ground_truth[r.transaction_id]
        gt_cat = gt["ground_truth_category"]
        llm_ok = r.category == gt_cat
        heur_ok = r.heuristic_category == gt_cat
        cat_llm += llm_ok
        cat_heur += heur_ok
        disagreements += (not r.agrees_with_heuristic)
        if gt["twist"]:
            n_twist += 1
            cat_llm_twist += llm_ok
            cat_heur_twist += heur_ok
        else:
            n_clean += 1
            cat_llm_clean += llm_ok
            cat_heur_clean += heur_ok

    # -- outcome / source / guardrail tallies ----------------------
    outcome_counts = Counter(r.final_outcome for r in results)
    source_counts = Counter(r.classification_source for r in results)
    rule_counts = Counter(rule for r in results for rule in r.rules_fired)

    amount_at_risk = round(sum(r.amount for r in results), 2)
    amount_recovered = round(sum(r.amount_recovered for r in results), 2)

    return {
        "run": {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "classifier_backend": active_backend(),
            "classifier_model": (
                config.CLASSIFIER_MODEL if active_backend() == "claude"
                else config.OLLAMA_MODEL if active_backend() == "ollama"
                else "offline-heuristic"
            ),
            "records": total,
            "max_retry_attempts": config.MAX_RETRY_ATTEMPTS,
            "cooldown_hours": config.COOLDOWN_HOURS,
        },
        "headline": {
            "recovery_rate": _rate(len(recovered), total),
            "recovered_count": len(recovered),
            "total_count": total,
            "amount_at_risk_inr": amount_at_risk,
            "amount_recovered_inr": amount_recovered,
            "value_recovery_rate": _rate(int(amount_recovered), int(amount_at_risk)),
        },
        "outcomes": dict(outcome_counts),
        "recovery_by_failure_reason": by_reason,
        "recovery_by_category": by_category,
        "hard_decline_restraint": {
            "ground_truth_leave_alone": len(gt_leave_alone),
            "correctly_left_alone": len(correctly_left_alone),
            "hard_declines_retried": len(hard_declines_retried),
            "restraint_rate": _rate(len(correctly_left_alone), len(gt_leave_alone)),
        },
        "false_positive_cost": {
            "transactions_with_wasted_retries": len(wasted),
            "wasted_retry_attempts": wasted_retry_attempts,
            "cost_per_wasted_retry_inr": config.WASTED_RETRY_COST_INR,
            "estimated_cost_inr": round(wasted_retry_attempts * config.WASTED_RETRY_COST_INR, 2),
            "wasted_transaction_ids": [r.transaction_id for r in wasted],
        },
        "decision_to_retry": {
            "true_positive": tp, "false_positive": fp,
            "false_negative": fn, "true_negative": tn,
            "precision": precision, "recall": recall, "f1": f1,
        },
        "best_action_agreement": {
            "match": best_action_match,
            "total": total,
            "rate": _rate(best_action_match, total),
        },
        "classifier_quality": {
            "category_accuracy_llm": _rate(cat_llm, total),
            "category_accuracy_heuristic": _rate(cat_heur, total),
            "lift": _rate(cat_llm - cat_heur, total),
            "twist_records": n_twist,
            "twist_accuracy_llm": _rate(cat_llm_twist, n_twist),
            "twist_accuracy_heuristic": _rate(cat_heur_twist, n_twist),
            "clean_accuracy_llm": _rate(cat_llm_clean, n_clean),
            "clean_accuracy_heuristic": _rate(cat_heur_clean, n_clean),
            "llm_heuristic_disagreements": disagreements,
            "disagreement_rate": _rate(disagreements, total),
        },
        "classification_sources": dict(source_counts),
        "guardrail_activity": dict(rule_counts),
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_report(summary: dict) -> str:
    run = summary["run"]
    h = summary["headline"]
    r2r = summary["decision_to_retry"]
    hd = summary["hard_decline_restraint"]
    fp = summary["false_positive_cost"]
    cq = summary["classifier_quality"]

    lines = [
        "# Payment Failure Recovery Agent - Batch Report",
        "",
        f"- **Run:** {run['timestamp']}",
        f"- **Classifier backend:** `{run['classifier_backend']}` ({run['classifier_model']})",
        f"- **Records processed:** {run['records']}",
        f"- **Guardrails:** max {run['max_retry_attempts']} retries, "
        f"{run['cooldown_hours']}h cooldown",
        "",
        "## Headline",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Recovery rate (count) | **{h['recovery_rate']*100:.1f}%** "
        f"({h['recovered_count']}/{h['total_count']}) |",
        f"| Amount at risk | Rs {h['amount_at_risk_inr']:,.0f} |",
        f"| Amount recovered (simulated) | **Rs {h['amount_recovered_inr']:,.0f}** |",
        f"| Value recovery rate | {h['value_recovery_rate']*100:.1f}% |",
        "",
        "## Outcomes",
        "",
        "| Outcome | Count |",
        "|---|---|",
    ]
    for outcome, count in sorted(summary["outcomes"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {outcome} | {count} |")

    lines += [
        "",
        "## Decision-to-retry accuracy",
        "",
        "Scored against `ground_truth_recoverable` - did the agent retry the",
        "things a retry could actually recover, and leave the rest alone?",
        "",
        "| | Value |",
        "|---|---|",
        f"| Precision | **{r2r['precision']*100:.1f}%** |",
        f"| Recall | **{r2r['recall']*100:.1f}%** |",
        f"| F1 | {r2r['f1']*100:.1f}% |",
        f"| TP / FP / FN / TN | {r2r['true_positive']} / {r2r['false_positive']} / "
        f"{r2r['false_negative']} / {r2r['true_negative']} |",
        "",
        f"**Best-action agreement:** {summary['best_action_agreement']['match']}/"
        f"{summary['best_action_agreement']['total']} "
        f"({summary['best_action_agreement']['rate']*100:.1f}%) - agent's first action "
        "matched `ground_truth_best_action`.",
        "",
        "## Classifier quality - context-aware LLM vs. flat lookup",
        "",
        "Category accuracy against `ground_truth_category`. The dataset is ~25% "
        "context-dependent cases where `failure_reason` alone is misleading.",
        "",
        "| | LLM | flat heuristic |",
        "|---|---|---|",
        f"| Overall | **{cq['category_accuracy_llm']*100:.1f}%** | {cq['category_accuracy_heuristic']*100:.1f}% |",
        f"| On clean records | {cq['clean_accuracy_llm']*100:.1f}% | {cq['clean_accuracy_heuristic']*100:.1f}% |",
        f"| On twist records ({cq['twist_records']}) | **{cq['twist_accuracy_llm']*100:.1f}%** | {cq['twist_accuracy_heuristic']*100:.1f}% |",
        "",
        f"LLM / heuristic disagreed on **{cq['llm_heuristic_disagreements']}** records "
        f"({cq['disagreement_rate']*100:.1f}%).",
        "",
        "## Hard-decline restraint",
        "",
        f"- Transactions that must be left alone (ground truth): **{hd['ground_truth_leave_alone']}**",
        f"- Correctly left alone by the agent: **{hd['correctly_left_alone']}** "
        f"({hd['restraint_rate']*100:.1f}%)",
        f"- Hard declines wrongly retried: **{hd['hard_declines_retried']}**",
        "",
        "## False-positive cost",
        "",
        f"- Transactions where a retry was spent on a non-recoverable failure: "
        f"**{fp['transactions_with_wasted_retries']}**",
        f"- Total wasted retry attempts: **{fp['wasted_retry_attempts']}**",
        f"- Estimated cost: **Rs {fp['estimated_cost_inr']:,.2f}** "
        f"(@ Rs {fp['cost_per_wasted_retry_inr']}/attempt)",
        "",
        "## Recovery rate by failure reason",
        "",
        "| failure_reason | recovered / total | rate | Rs recovered |",
        "|---|---|---|---|",
    ]
    for reason, s in summary["recovery_by_failure_reason"].items():
        lines.append(
            f"| {reason} | {s['recovered']}/{s['total']} | {s['recovery_rate']*100:.0f}% | "
            f"Rs {s['amount_recovered']:,.0f} |"
        )

    lines += [
        "",
        "## Recovery rate by category (classifier)",
        "",
        "| category | recovered / total | rate |",
        "|---|---|---|",
    ]
    for cat, s in summary["recovery_by_category"].items():
        lines.append(f"| {cat} | {s['recovered']}/{s['total']} | {s['recovery_rate']*100:.0f}% |")

    lines += [
        "",
        "## Guardrail activity (rule fires across all decisions)",
        "",
        "| rule | fires |",
        "|---|---|",
    ]
    for rule, count in sorted(summary["guardrail_activity"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{rule}` | {count} |")

    lines += [
        "",
        f"_Classification sources: {summary['classification_sources']}_",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    with open(DATA_PATH, encoding="utf-8") as fh:
        records = json.load(fh)
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as fh:
        ground_truth = json.load(fh)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    run_id = datetime.now().strftime("run_%Y%m%dT%H%M%S")
    audit_path = os.path.join(RESULTS_DIR, "audit_log.jsonl")

    print(f"Backend: {active_backend()}   records: {len(records)}   run_id: {run_id}")
    print("Processing...")

    rng = random.Random(SIMULATION_SEED)
    results: list[RecoveryResult] = []
    with AuditLogger(audit_path, run_id) as logger:
        for i, rec in enumerate(records, 1):
            results.append(run_recovery(rec, rng=rng, logger=logger))
            if i % 25 == 0:
                print(f"  {i}/{len(records)}")
        audit_records = logger.records_written

    summary = score(results, ground_truth)

    summary_path = os.path.join(RESULTS_DIR, "summary.json")
    report_path = os.path.join(RESULTS_DIR, "report.md")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(render_report(summary))

    # also dump the per-transaction results for the dashboard's detail view
    with open(os.path.join(RESULTS_DIR, "transactions.json"), "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in results], fh, indent=2)

    h = summary["headline"]
    r2r = summary["decision_to_retry"]
    hd = summary["hard_decline_restraint"]
    fp = summary["false_positive_cost"]
    cq = summary["classifier_quality"]
    print(f"\n{'='*60}")
    print(f"Recovery rate      : {h['recovery_rate']*100:.1f}%  "
          f"({h['recovered_count']}/{h['total_count']})")
    print(f"Amount recovered   : Rs {h['amount_recovered_inr']:,.0f} "
          f"of Rs {h['amount_at_risk_inr']:,.0f}")
    print(f"Classifier accuracy: LLM {cq['category_accuracy_llm']*100:.1f}%  vs  "
          f"heuristic {cq['category_accuracy_heuristic']*100:.1f}%   "
          f"(twist cases: {cq['twist_accuracy_llm']*100:.0f}% vs {cq['twist_accuracy_heuristic']*100:.0f}%)")
    print(f"Retry decision     : precision {r2r['precision']*100:.1f}%  "
          f"recall {r2r['recall']*100:.1f}%  (FP={r2r['false_positive']})")
    print(f"Hard-decline restraint: {hd['correctly_left_alone']}/{hd['ground_truth_leave_alone']} "
          f"left alone, {hd['hard_declines_retried']} wrongly retried")
    print(f"False-positive cost: Rs {fp['estimated_cost_inr']:,.2f} "
          f"({fp['wasted_retry_attempts']} wasted retries)")
    print(f"{'='*60}")
    print(f"\nWrote:\n  {audit_path} ({audit_records} decisions)\n  {summary_path}\n  {report_path}")
    print("\nView the demo site with:  python -m uvicorn web.app:app --reload   (then open http://localhost:8000)")


if __name__ == "__main__":
    main()
