"""Classifier evaluation harness.

Runs the classifier over the whole dataset and scores it against the hidden
ground truth, with a hard focus on the thing that matters: does a context-aware
LLM actually beat the flat `failure_reason -> category` lookup, and where?

    python eval/run_eval.py                 # full run (uses the resolved backend)
    python eval/run_eval.py --limit 40      # quick sample (Ollama is slow on CPU)

Prints:
  * 5x5 category confusion matrix
  * per-category precision / recall / F1
  * overall category accuracy: LLM vs offline-heuristic baseline
  * accuracy split by clean vs context-dependent (twist) records
  * best-action accuracy (classifier -> policy engine -> action vs ground truth)
  * decision-to-retry precision / recall (vs ground_truth_recoverable)
  * LLM/heuristic disagreement rate, and who is right on the disagreements
  * confidence calibration + how many the confidence gate would escalate

Appends one line to eval/history.jsonl (keyed by prompt_version) so you can show
the accuracy trend as the prompt improves.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import config
from agent.classifier import PROMPT_VERSION, active_backend, classify
from agent.policy_engine import decide

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATS = list(config.CATEGORIES)


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="evaluate only the first N records")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "data", "failed_payments.json"), encoding="utf-8") as fh:
        records = json.load(fh)
    with open(os.path.join(ROOT, "eval", "ground_truth.json"), encoding="utf-8") as fh:
        ground_truth = json.load(fh)
    if args.limit:
        records = records[: args.limit]

    backend = active_backend()
    print(f"backend={backend}  prompt_version={PROMPT_VERSION}  "
          f"model={config.OLLAMA_MODEL if backend == 'ollama' else config.CLASSIFIER_MODEL}  "
          f"n={len(records)}\n")

    # confusion[actual][predicted]
    confusion: dict[str, Counter] = {c: Counter() for c in CATS}
    heur_confusion: dict[str, Counter] = {c: Counter() for c in CATS}

    n = 0
    cat_correct = heur_correct = 0
    cat_correct_clean = cat_correct_twist = 0
    n_clean = n_twist = 0
    action_correct = 0
    disagree = 0
    disagree_llm_right = disagree_heur_right = disagree_both_wrong = 0
    conf_sum = 0.0
    conf_correct = conf_total = lowconf_total = lowconf_would_escalate = 0
    retry_tp = retry_fp = retry_fn = retry_tn = 0
    misses: list[tuple] = []

    for i, rec in enumerate(records, 1):
        gt = ground_truth[rec["transaction_id"]]
        gt_cat = gt["ground_truth_category"]
        gt_action = gt["ground_truth_best_action"]
        gt_recoverable = gt["ground_truth_recoverable"]
        is_twist = bool(gt["twist"])

        cls = classify(rec)
        dec = decide(rec, cls)

        n += 1
        confusion[gt_cat][cls.category] += 1
        heur_confusion[gt_cat][cls.heuristic_category] += 1

        ok = cls.category == gt_cat
        heur_ok = cls.heuristic_category == gt_cat
        cat_correct += ok
        heur_correct += heur_ok
        if is_twist:
            n_twist += 1
            cat_correct_twist += ok
        else:
            n_clean += 1
            cat_correct_clean += ok
        if not ok:
            misses.append((rec["transaction_id"], gt["gt_rule"], gt_cat, cls.category,
                           cls.heuristic_category, round(cls.confidence, 2)))

        action_correct += dec.action == gt_action

        if not cls.agrees_with_heuristic:
            disagree += 1
            if ok and not heur_ok:
                disagree_llm_right += 1
            elif heur_ok and not ok:
                disagree_heur_right += 1
            elif not ok and not heur_ok:
                disagree_both_wrong += 1

        conf_sum += cls.confidence
        if cls.source in ("ollama", "claude"):
            conf_total += 1
            conf_correct += ok
            if cls.confidence < config.MIN_CONFIDENCE:
                lowconf_total += 1
                lowconf_would_escalate += 1

        predicted_retry = dec.action == "smart_retry"
        if predicted_retry and gt_recoverable:
            retry_tp += 1
        elif predicted_retry and not gt_recoverable:
            retry_fp += 1
        elif not predicted_retry and gt_recoverable:
            retry_fn += 1
        else:
            retry_tn += 1

        if i % 25 == 0:
            print(f"  ...{i}/{len(records)}")

    # ---- confusion matrix ------------------------------------------------
    print("\nCATEGORY CONFUSION  (rows = actual, cols = predicted by LLM)\n")
    short = {c: c[:9] for c in CATS}
    header = " " * 22 + "".join(f"{short[c]:>11}" for c in CATS)
    print(header)
    for a in CATS:
        row = "".join(f"{confusion[a][p]:>11}" for p in CATS)
        print(f"{a:>21} {row}   (n={sum(confusion[a].values())})")

    # ---- per-category P/R/F1 ------------------------------------------
    print("\nPER-CATEGORY  (LLM)\n")
    print(f"{'category':>22}  {'precision':>9} {'recall':>9} {'f1':>9}   support")
    for c in CATS:
        tp = confusion[c][c]
        fp = sum(confusion[a][c] for a in CATS if a != c)
        fn = sum(confusion[c][p] for p in CATS if p != c)
        p, r, f = _prf(tp, fp, fn)
        print(f"{c:>22}  {_pct(p):>9} {_pct(r):>9} {_pct(f):>9}   {sum(confusion[c].values())}")

    # heuristic accuracy split clean vs twist
    hc_clean = hc_twist = 0
    for rec in records:
        gt = ground_truth[rec["transaction_id"]]
        hcat = config.FALLBACK_REASON_TO_CATEGORY.get(
            rec["failure_reason"], "needs_customer_action")
        if hcat == gt["ground_truth_category"]:
            if gt["twist"]:
                hc_twist += 1
            else:
                hc_clean += 1

    # ---- headline -----------------------------------------------------
    print("\nHEADLINE\n")
    print(f"  category accuracy  (LLM)        : {_pct(cat_correct / n)}  ({cat_correct}/{n})")
    print(f"  category accuracy  (heuristic)  : {_pct(heur_correct / n)}  ({heur_correct}/{n})")
    print(f"  lift from context-aware LLM     : {_pct((cat_correct - heur_correct) / n)}")
    if n_twist:
        print(f"    on twist records ({n_twist:>3}) : LLM {_pct(cat_correct_twist / n_twist)}  "
              f"heuristic {_pct(hc_twist / n_twist)}   <- the whole point")
    if n_clean:
        print(f"    on clean records ({n_clean:>3}) : LLM {_pct(cat_correct_clean / n_clean)}  "
              f"heuristic {_pct(hc_clean / n_clean)}")

    print(f"\n  best-action accuracy (via policy engine) : "
          f"{_pct(action_correct / n)}  ({action_correct}/{n})")

    p, r, f = _prf(retry_tp, retry_fp, retry_fn)
    print(f"  decision-to-retry  precision {_pct(p)}  recall {_pct(r)}  f1 {_pct(f)}  "
          f"(TP {retry_tp} / FP {retry_fp} / FN {retry_fn} / TN {retry_tn})")

    print("\nLLM vs HEURISTIC DISAGREEMENT\n")
    print(f"  disagreed on {disagree}/{n} records ({_pct(disagree / n)})")
    if disagree:
        print(f"    LLM right, heuristic wrong : {disagree_llm_right}")
        print(f"    heuristic right, LLM wrong : {disagree_heur_right}")
        print(f"    both wrong                 : {disagree_both_wrong}")

    print("\nCONFIDENCE\n")
    print(f"  mean confidence                 : {conf_sum / n:.2f}")
    if conf_total:
        print(f"  accuracy on LLM calls           : {_pct(conf_correct / conf_total)}")
        print(f"  below {config.MIN_CONFIDENCE:.2f} threshold          : "
              f"{lowconf_total}/{conf_total}  (policy escalates these)")

    if misses:
        print(f"\nMISSES ({len(misses)})  txn / gt_rule / actual -> predicted (heuristic) conf\n")
        for m in misses[:30]:
            print(f"  {m[0]}  {m[1]:<26} {m[2]:>20} -> {m[3]:<20} (heur {m[4]}) {m[5]}")
        if len(misses) > 30:
            print(f"  ... and {len(misses) - 30} more")

    # ---- history + report -------------------------------------------
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "prompt_version": PROMPT_VERSION,
        "backend": backend,
        "model": config.OLLAMA_MODEL if backend == "ollama" else config.CLASSIFIER_MODEL,
        "n": n,
        "category_accuracy": round(cat_correct / n, 4),
        "category_accuracy_heuristic": round(heur_correct / n, 4),
        "category_accuracy_twist": round(cat_correct_twist / n_twist, 4) if n_twist else None,
        "category_accuracy_clean": round(cat_correct_clean / n_clean, 4) if n_clean else None,
        "best_action_accuracy": round(action_correct / n, 4),
        "retry_precision": round(p, 4),
        "retry_recall": round(r, 4),
        "disagreement_rate": round(disagree / n, 4),
    }
    hist_path = os.path.join(ROOT, "eval", "history.jsonl")
    with open(hist_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    with open(os.path.join(ROOT, "eval", "last_report.json"), "w", encoding="utf-8") as fh:
        json.dump(row, fh, indent=2)
    print(f"\nappended -> {hist_path}")


if __name__ == "__main__":
    main()
