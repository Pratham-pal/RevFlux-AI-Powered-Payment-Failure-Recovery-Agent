"""Single source of truth for every tunable in the recovery pipeline.

Keeping these in one file (rather than scattered as literals) is intentional:
in the panel interview each number here is a business-rule decision I need to be
able to point at and defend.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# LLM classifier backend
# ---------------------------------------------------------------------------
#
# The classifier (agent/classifier.py) can get its judgment from three places:
#
#   "ollama"   -> a local model via Ollama. Free, no network, real reasoning.
#   "claude"   -> the Anthropic API. Best quality, costs ~a few rupees per run.
#   "offline"  -> a flat failure_reason -> category lookup. Free, no reasoning.
#   "auto"     -> try ollama, then claude, then offline (whatever is reachable).
#
# Whatever backend is chosen, any hard failure at runtime falls back to
# "offline" so the pipeline always completes.
CLASSIFIER_BACKEND = os.environ.get("RECOVERY_AGENT_BACKEND", "auto").lower()

# Local (Ollama) settings.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT_S", "120"))

# Anthropic API model; override with CLASSIFIER_MODEL.
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-opus-5")

# Hard override: force the offline heuristic regardless of CLASSIFIER_BACKEND.
# (Handy for a deterministic demo run or when the venue wifi dies.)
FORCE_OFFLINE = os.environ.get("RECOVERY_AGENT_OFFLINE", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Classification taxonomy  (this is the LLM's entire decision space)
# ---------------------------------------------------------------------------

CATEGORIES = (
    "hard_decline",           # never retry
    "soft_recoverable",       # retry the same charge, with smart timing
    "needs_reauth",           # mandate / authorization must be re-established
    "needs_customer_action",  # customer must update or re-enter something
    "needs_review",           # ambiguous / chronic / possible false decline -> a human decides
)

# ---------------------------------------------------------------------------
# Action vocabulary  (this is the policy engine's entire output space)
# ---------------------------------------------------------------------------

ACTIONS = (
    "smart_retry",
    "request_mandate_reauth",
    "nudge_customer",
    "escalate_manual_review",
    "no_action",
)

# ---------------------------------------------------------------------------
# Hard guardrails  (deterministic, NOT LLM-controlled)
# ---------------------------------------------------------------------------

# Absolute ceiling on retry attempts for a single charge, across its whole
# lifetime (retries that happened before us + retries we schedule).
MAX_RETRY_ATTEMPTS = 3

# Minimum gap between two retry attempts on the same charge. No back-to-back
# retries -- this is the "cooldown / stopping rules" requirement.
COOLDOWN_HOURS = 4

# The classifier reports a self-assessed confidence (0..1). Below this, the
# policy engine escalates to manual review instead of acting on the label --
# a shaky call becomes safe human review, not a wrong automated action.
# Only applied to real LLM backends (the offline heuristic's confidence is not
# a calibrated signal).
MIN_CONFIDENCE = float(os.environ.get("RECOVERY_MIN_CONFIDENCE", "0.6"))

# Failure reasons that must NEVER be retried, enforced independently of what
# the LLM says. If the classifier ever returns something other than
# `hard_decline` for one of these, the policy engine still blocks the retry
# and logs the disagreement.
HARD_DECLINE_REASONS = frozenset({
    "card_blocked",
    "issuer_declined_fraud_suspected",
    "card_reported_stolen",
})

# Failure reasons a resubmit of the same charge can plausibly clear.
SOFT_RECOVERABLE_REASONS = frozenset({
    "insufficient_funds", "bank_timeout", "network_error",
})
# ... of which these are usually just transient (not balance-driven).
TRANSIENT_REASONS = frozenset({"bank_timeout", "network_error"})

# ---------------------------------------------------------------------------
# Smart-retry timing
# ---------------------------------------------------------------------------

# insufficient_funds: retry 24-48h later, and if a salary-credit day (1st-5th)
# falls inside the 24-72h window, snap the retry onto it.
SALARY_CREDIT_DAYS = (1, 2, 3, 4, 5)
SALARY_RETRY_HOUR = 10  # 10:00 local -- after typical morning salary credits

# Transient failures: retry after a fixed short delay (still >= COOLDOWN_HOURS).
TRANSIENT_RETRY_HOURS = {
    "network_error": COOLDOWN_HOURS,  # often clears immediately; cooldown is the floor
    "bank_timeout": 6,
}

# ---------------------------------------------------------------------------
# Simulated executor  (no real payment calls are ever made)
# ---------------------------------------------------------------------------
#
# P(a recovery attempt succeeds) = INTERVENTION_EFFECTIVENESS[action][category]
#
# The agent ALWAYS pairs the fitting action with the category (the diagonal):
#   soft_recoverable -> smart_retry (0.60),  needs_reauth -> request_mandate_reauth
#   (0.70),  needs_customer_action -> nudge_customer (0.40),  hard_decline -> no action.
#
# A naive "retry everything" baseline hits the OFF-diagonal -- it retries expired
# mandates, dead cards and blocked cards -- where a blind retry almost never
# works. That gap (plus the wasted spend and the extra declines pushed at the
# issuer) is what the agent-vs-naive comparison shows.
INTERVENTION_EFFECTIVENESS = {
    "smart_retry": {
        "soft_recoverable": 0.60,
        "needs_reauth": 0.03,
        "needs_customer_action": 0.03,
        "hard_decline": 0.0,
    },
    "request_mandate_reauth": {
        "needs_reauth": 0.70,
        "soft_recoverable": 0.10,
        "needs_customer_action": 0.10,
        "hard_decline": 0.0,
    },
    "nudge_customer": {
        "needs_customer_action": 0.40,
        "needs_reauth": 0.15,
        "soft_recoverable": 0.10,
        "hard_decline": 0.0,
    },
}

# ---------------------------------------------------------------------------
# Economics  (used only for post-hoc reporting)
# ---------------------------------------------------------------------------

# Blended cost of a wasted retry attempt: network / MDR fees on the re-auth
# plus the soft cost of pushing more declines at an issuer (hurts future auth
# rates). A rough, defensible placeholder.
WASTED_RETRY_COST_INR = 7.0

# Reference "now" -- matches the dataset generator so scheduled retry dates and
# cooldown math are reproducible.
REFERENCE_NOW_ISO = "2026-08-27T09:00:00"

# ---------------------------------------------------------------------------
# Offline heuristic fallback
# ---------------------------------------------------------------------------
# Only used when the LLM is unreachable. In normal operation the LLM makes this
# call with full context; this flat map is the degraded-mode safety net so the
# pipeline still runs end-to-end for a demo.

FALLBACK_REASON_TO_CATEGORY = {
    "insufficient_funds": "soft_recoverable",
    "bank_timeout": "soft_recoverable",
    "network_error": "soft_recoverable",
    "expired_mandate": "needs_reauth",
    "wrong_cvv": "needs_customer_action",
    "card_expired": "needs_customer_action",
    "invalid_otp": "needs_customer_action",
    "card_blocked": "hard_decline",
    "issuer_declined_fraud_suspected": "hard_decline",
    "card_reported_stolen": "hard_decline",
}
