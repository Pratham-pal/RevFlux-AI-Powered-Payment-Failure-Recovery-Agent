"""Stage 4: the audit trail.

Every decision the pipeline makes for every transaction is written here as one
JSON line. This is the artifact that answers Razorpay's "compliant escalation,
stopping rules -- visibly logged for every transaction" requirement: nothing the
agent does is invisible, and each line carries the reasoning and the exact rule
ids that produced the action.

One line == one decision (not one transaction). A transaction that is retried
twice then escalated produces three lines, all sharing its transaction_id.
"""

from __future__ import annotations

import json
from datetime import datetime

from agent.classifier import Classification
from agent.policy_engine import Decision


class AuditLogger:
    """Append-only JSON-lines writer for pipeline decisions."""

    def __init__(self, path: str, run_id: str):
        self.path = path
        self.run_id = run_id
        self._fh = open(path, "w", encoding="utf-8")
        self._count = 0

    def log(
        self,
        *,
        transaction_id: str,
        failure_reason: str,
        amount: float,
        attempt_number: int,
        classification: Classification,
        decision: Decision,
        outcome: str,
        simulated_amount_recovered: float,
    ) -> None:
        """Write one decision record."""
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "transaction_id": transaction_id,
            "failure_reason": failure_reason,
            "amount": round(amount, 2),
            "attempt_number": attempt_number,
            "category": classification.category,
            "classification_source": classification.source,
            "classification_confidence": round(classification.confidence, 2),
            "heuristic_category": classification.heuristic_category,
            "agrees_with_heuristic": classification.agrees_with_heuristic,
            "reasoning": classification.reasoning,
            "action_taken": decision.action,
            "retry_delay_hours": decision.retry_delay_hours,
            "scheduled_retry_at": decision.scheduled_retry_at,
            "rules_applied": decision.rules_applied,
            "guardrail_triggered": decision.guardrail_triggered,
            "policy_rationale": decision.rationale,
            "outcome": outcome,
            "simulated_amount_recovered": round(simulated_amount_recovered, 2),
        }
        self._fh.write(json.dumps(record) + "\n")
        self._count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def records_written(self) -> int:
        return self._count
