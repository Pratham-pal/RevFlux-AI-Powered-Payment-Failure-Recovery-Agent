"""Stage 1 of the pipeline: the LLM judgment call.

The classifier reads ONE failed payment event and decides which of five recovery
categories it belongs to, plus a short reasoning string and a self-assessed
confidence. That is the whole job.

What the classifier deliberately does NOT do:
  * it never picks the concrete action (smart_retry / nudge / ...)
  * it never reasons about retry caps, cooldowns, or escalation
  * its output can never, on its own, cause a retry to be executed

All of that lives in `policy_engine.py` as deterministic code. This separation
is the project's central safety argument: a hallucinated or adversarial
classification can at worst mislabel a category; it cannot bypass a retry cap
or retry a blocked card, because the policy engine re-checks those in Python.

Backends (config.CLASSIFIER_BACKEND):
  "ollama"  - local model via Ollama HTTP API (free, offline-capable, real reasoning)
  "claude"  - Anthropic API (best quality, paid)
  "offline" - flat failure_reason -> category lookup (free, no reasoning)
  "auto"    - ollama, then claude, then offline

Any hard failure at runtime degrades to the offline heuristic for the rest of
the process. Every result is tagged with its `source` and carries the offline
heuristic's opinion too (`heuristic_category` / `agrees_with_heuristic`) so the
eval harness can measure how often, and where, the LLM beats the flat lookup.
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

from agent import config

try:  # the Anthropic SDK is optional; absence just removes the "claude" backend
    import anthropic
except ImportError:  # pragma: no cover - depends on install state
    anthropic = None  # type: ignore

# Bump this whenever the prompt / few-shots change, so eval history is comparable.
PROMPT_VERSION = "v3_fewshot_context_fixed"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    category: str               # one of config.CATEGORIES
    reasoning: str
    confidence: float           # 0..1, self-reported
    source: str                 # "ollama" | "claude" | "fallback"
    heuristic_category: str = ""       # what the flat lookup would have said
    agrees_with_heuristic: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Prompt + shared JSON schema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the classification stage of a payment-failure recovery system for an \
Indian payment gateway (Razorpay-style).

Your ONLY job: read one failed payment event and decide which recovery CATEGORY \
it belongs to, explain why in one or two sentences, and give a confidence. You \
do NOT choose the concrete action, the retry timing, or whether any retry limit \
has been hit -- a separate deterministic policy engine owns all of that.

The five categories:

- hard_decline: the issuer/bank has actively refused in a way that will not \
change on retry -- card blocked, card reported stolen, or an issuer decline with \
fraud suspicion. Retrying is pointless and looks abusive to the issuer.

- soft_recoverable: the payment failed for a transient or balance-timing reason \
that resubmitting the SAME charge later can plausibly clear -- insufficient \
funds, a bank/switch timeout, or a network error -- AND nothing in the context \
says otherwise.

- needs_reauth: the instrument's mandate/authorization is no longer valid and \
must be re-established before ANY charge can succeed -- an expired UPI AutoPay / \
e-mandate on a subscription. A blind retry cannot fix this.

- needs_customer_action: the charge cannot succeed until the customer updates or \
re-enters something -- wrong CVV, an expired card, or an invalid OTP (single-use, \
already consumed). Includes cases where the failure code says "timeout" or \
"network error" but the instrument itself is expired -- the customer still has \
to fix the card.

- needs_review: the situation is ambiguous, chronic, or a likely false decline, \
and a human should decide. Use this when: the customer has failed for the SAME \
reason many times recently (retrying again won't help); a "transient" failure \
has already survived two retries on a thin-history account (it isn't transient); \
a fraud-suspected decline lands on a long, clean, established account (possible \
false positive -- but still never auto-retry a fraud flag); or the failure code \
contradicts the transaction type (e.g. an expired *mandate* on a one-off charge).

Reason in this order:
  1. Is the payment instrument itself still valid? (not expired, not blocked)
  2. Is this a genuine one-off transient failure, or a repeating pattern?
  3. Does the context (history, prior retries, transaction type, instrument
     status) contradict what the raw failure code suggests?

  IMPORTANT: two signals outrank the raw failure_reason every time --
  (a) recent_failures_same_reason >= 3 in the recent window always means a
  chronic pattern, not a one-off, regardless of which reason it is; and
  (b) transaction_type must match what the failure_reason implies -- an
  expired_mandate only makes sense on transaction_type=subscription (it is a
  recurring-payment concept); if transaction_type is one_time, that mismatch
  itself is the signal to escalate. Never let the failure_reason alone decide
  needs_reauth -- check the transaction_type agrees with it first.

  4. If it is ambiguous or needs investigation, choose needs_review. Never
     guess an auto-retry when unsure.

Examples:

EVENT: failure_reason=insufficient_funds; payment_method=card; \
transaction_type=one_time; amount=INR 2,400; retry_count_so_far=0; \
instrument_status=ok; recent failures with this same reason=0 in 14 days; \
history=22 ok / 1 fail, 400d old
-> {"category":"soft_recoverable","confidence":0.9,"reasoning":"A one-off \
balance shortfall on an otherwise healthy account; resubmitting the same charge \
after a short wait can clear."}

EVENT: failure_reason=card_blocked; payment_method=card; \
transaction_type=one_time; amount=INR 1,499; retry_count_so_far=0; \
instrument_status=card_blocked; recent failures with this same reason=0 in 14 \
days; history=30 ok / 3 fail, 1600d old
-> {"category":"hard_decline","confidence":0.98,"reasoning":"The issuer has \
blocked the card; no retry will succeed and repeated attempts look abusive."}

EVENT: failure_reason=expired_mandate; payment_method=upi; \
transaction_type=subscription; amount=INR 499; retry_count_so_far=0; \
instrument_status=mandate_expired; recent failures with this same reason=1 in 14 \
days; history=12 ok / 2 fail, 500d old
-> {"category":"needs_reauth","confidence":0.95,"reasoning":"The UPI AutoPay \
mandate has lapsed; the customer must re-authorize it before any charge can go \
through."}

EVENT: failure_reason=wrong_cvv; payment_method=card; transaction_type=one_time; \
amount=INR 6,200; retry_count_so_far=1; instrument_status=ok; recent failures \
with this same reason=0 in 14 days; history=8 ok / 1 fail, 300d old
-> {"category":"needs_customer_action","confidence":0.92,"reasoning":"A wrong \
CVV cannot be fixed by resubmitting the same charge; the customer must re-enter \
correct card details."}

EVENT: failure_reason=bank_timeout; payment_method=card; \
transaction_type=one_time; amount=INR 3,100; retry_count_so_far=0; \
instrument_status=card_expired; recent failures with this same reason=0 in 14 \
days; history=15 ok / 2 fail, 700d old
-> {"category":"needs_customer_action","confidence":0.85,"reasoning":"The \
timeout masks the real problem: the card is expired. A retry cannot succeed \
until the customer updates the card."}

EVENT: failure_reason=insufficient_funds; payment_method=upi; \
transaction_type=subscription; amount=INR 999; retry_count_so_far=1; \
instrument_status=ok; recent failures with this same reason=4 in 9 days; \
history=6 ok / 7 fail, 300d old
-> {"category":"needs_review","confidence":0.8,"reasoning":"Four insufficient- \
funds failures in nine days is a chronic shortfall, not a timing issue; a human \
should decide whether to pause billing rather than keep retrying."}

EVENT: failure_reason=issuer_declined_fraud_suspected; payment_method=card; \
transaction_type=one_time; amount=INR 7,800; retry_count_so_far=0; \
instrument_status=ok; recent failures with this same reason=0 in 14 days; \
history=63 ok / 1 fail, 1400d old
-> {"category":"needs_review","confidence":0.75,"reasoning":"A fraud-suspected \
decline on a 4-year account with 63 clean payments is likely a false positive; \
route to fast human review -- but never auto-retry a fraud flag."}

EVENT: failure_reason=network_error; payment_method=netbanking; \
transaction_type=one_time; amount=INR 1,050; retry_count_so_far=2; \
instrument_status=ok; recent failures with this same reason=2 in 6 days; \
history=1 ok / 3 fail, 20d old
-> {"category":"needs_review","confidence":0.78,"reasoning":"A 'network error' \
that has already survived two retries on a brand-new thin-history account is \
probably not transient; escalate instead of burning the last retry."}

EVENT: failure_reason=insufficient_funds; payment_method=upi; \
transaction_type=subscription; amount=INR 299; retry_count_so_far=1; \
instrument_status=ok; recent failures with this same reason=3 in 8 days; \
history=9 ok / 4 fail, 250d old
-> {"category":"needs_review","confidence":0.85,"reasoning":"Three \
insufficient-funds failures in eight days is a repeating shortfall pattern, \
not a single timing miss; a human should decide on billing rather than \
scheduling another automatic retry."}

EVENT: failure_reason=expired_mandate; payment_method=upi; \
transaction_type=one_time; amount=INR 1,800; retry_count_so_far=0; \
instrument_status=mandate_expired; recent failures with this same reason=0 in \
14 days; history=18 ok / 1 fail, 600d old
-> {"category":"needs_review","confidence":0.9,"reasoning":"expired_mandate \
only applies to recurring UPI AutoPay, but this is a one_time transaction -- \
the failure code doesn't match the transaction type, which is itself the \
signal to route to a human rather than treat it as a routine mandate lapse."}

EVENT: failure_reason=card_expired; payment_method=card; \
transaction_type=subscription; amount=INR 599; retry_count_so_far=0; \
instrument_status=card_expired; recent failures with this same reason=0 in 14 \
days; history=20 ok / 2 fail, 500d old
-> {"category":"needs_customer_action","confidence":0.93,"reasoning":"The \
card itself has expired -- that is a card problem, not a mandate problem, \
even though the transaction is a subscription. The customer must update the \
card; this is not needs_reauth, which is only for a lapsed UPI mandate."}

EVENT: failure_reason=issuer_declined_fraud_suspected; payment_method=card; \
transaction_type=one_time; amount=INR 2,000; retry_count_so_far=0; \
instrument_status=ok; recent failures with this same reason=0 in 14 days; \
history=1 ok / 0 fail, 30d old
-> {"category":"hard_decline","confidence":0.9,"reasoning":"A fraud flag on a \
brand-new, thin-history account is not the false-positive pattern -- that \
exception only applies to long-established, heavily clean accounts. Here \
there is not enough history to override the issuer's fraud signal, so this \
is a straightforward hard decline."}"""

_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reasoning", "category", "confidence"],
    "properties": {
        "reasoning": {"type": "string",
                      "description": "One or two sentences explaining the category choice."},
        "category": {"type": "string", "enum": list(config.CATEGORIES)},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "Self-reported confidence in the category, 0..1."},
    },
}


def _format_event(event: dict) -> str:
    h = event["customer_history"]
    return (
        "EVENT: "
        f"failure_reason={event['failure_reason']}; "
        f"payment_method={event['payment_method']}; "
        f"transaction_type={event['transaction_type']}; "
        f"amount=INR {event['amount']:,.2f}; "
        f"retry_count_so_far={event['retry_count_so_far']}; "
        f"instrument_status={event.get('instrument_status', 'ok')}; "
        f"recent failures with this same reason={h.get('recent_failures_same_reason', 0)} "
        f"in {h.get('recent_failure_window_days', 14)} days; "
        f"history={h['prior_successful_payments']} ok / {h['prior_failures']} fail, "
        f"{h['account_age_days']}d old"
    )


def heuristic_category(event: dict) -> str:
    """What the flat failure_reason -> category lookup would say (no context)."""
    return config.FALLBACK_REASON_TO_CATEGORY.get(
        event["failure_reason"], "needs_customer_action")


def _coerce_result(data: dict, source: str) -> Classification:
    category = data.get("category")
    if category not in config.CATEGORIES:
        raise ValueError(f"{source} returned unknown category: {category!r}")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Classification(
        category=category,
        reasoning=str(data.get("reasoning", "")).strip(),
        confidence=max(0.0, min(1.0, confidence)),
        source=source,
    )


# ---------------------------------------------------------------------------
# Backend: Ollama (local)
# ---------------------------------------------------------------------------

def _classify_ollama(event: dict) -> Classification:
    payload = {
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "format": _RESULT_SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _format_event(event)},
        ],
    }
    req = urllib.request.Request(
        f"{config.OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["message"]["content"]
    data = json.loads(content) if isinstance(content, str) else content
    return _coerce_result(data, source="ollama")


def _ollama_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{config.OLLAMA_HOST}/api/tags", timeout=3) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Backend: Anthropic API (Claude)
# ---------------------------------------------------------------------------

_anthropic_client = None

_CLAUDE_TOOL = {
    "name": "submit_classification",
    "description": "Record the recovery category for this failed payment event.",
    "strict": True,
    "input_schema": _RESULT_SCHEMA,
}


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _classify_claude(event: dict) -> Classification:
    resp = _get_anthropic_client().messages.create(
        model=config.CLASSIFIER_MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _format_event(event)}],
        tools=[_CLAUDE_TOOL],
        tool_choice={"type": "tool", "name": "submit_classification"},
    )
    tool_block = next(b for b in resp.content if b.type == "tool_use")
    data = tool_block.input
    if isinstance(data, str):
        data = json.loads(data)
    return _coerce_result(data, source="claude")


# ---------------------------------------------------------------------------
# Backend: offline heuristic
# ---------------------------------------------------------------------------

def _classify_offline(event: dict) -> Classification:
    """Degraded-mode classifier: flat failure_reason -> category lookup.

    A safety net, not the design. It has no access to context nuance -- it
    cannot see an expired instrument, a chronic-failure pattern, or a likely
    false fraud flag -- which is exactly why a real model does this job in
    normal operation, and exactly what the eval harness quantifies.
    """
    reason = event["failure_reason"]
    category = heuristic_category(event)
    return Classification(
        category=category,
        reasoning=f"[offline heuristic] mapped failure_reason '{reason}' -> {category}.",
        confidence=0.5,
        source="fallback",
    )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_active_backend: str | None = None


def _resolve_backend() -> str:
    if config.FORCE_OFFLINE:
        return "offline"
    choice = config.CLASSIFIER_BACKEND
    if choice in ("ollama", "claude", "offline"):
        return choice
    if _ollama_reachable():
        return "ollama"
    if anthropic is not None and (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        return "claude"
    return "offline"


_BACKENDS = {
    "ollama": _classify_ollama,
    "claude": _classify_claude,
    "offline": _classify_offline,
}


def active_backend() -> str:
    global _active_backend
    if _active_backend is None:
        _active_backend = _resolve_backend()
    return _active_backend


def classify(event: dict) -> Classification:
    """Classify one failed payment event; attach the heuristic's opinion too."""
    global _active_backend

    backend = active_backend()
    if backend == "offline":
        result = _classify_offline(event)
    else:
        try:
            result = _BACKENDS[backend](event)
        except Exception as exc:  # noqa: BLE001 - any failure -> safe degraded mode
            _active_backend = "offline"
            detail = getattr(exc, "reason", exc)
            print(f"  [classifier] backend '{backend}' unavailable "
                  f"({type(exc).__name__}: {detail}); using the offline heuristic "
                  "for the rest of this run.")
            result = _classify_offline(event)

    hc = heuristic_category(event)
    return dataclasses.replace(
        result, heuristic_category=hc, agrees_with_heuristic=(result.category == hc))


# ---------------------------------------------------------------------------
# Self-demo:  python -m agent.classifier
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(os.path.dirname(here), "data", "failed_payments.json")
    gt_path = os.path.join(os.path.dirname(here), "eval", "ground_truth.json")
    with open(data_path, encoding="utf-8") as fh:
        records = json.load(fh)
    with open(gt_path, encoding="utf-8") as fh:
        gt = json.load(fh)

    print(f"backend: {active_backend()}   prompt: {PROMPT_VERSION}   "
          f"ollama_model={config.OLLAMA_MODEL}\n")

    # one clean example per reason + every twist rule
    shown: set[str] = set()
    for rec in records:
        g = gt[rec["transaction_id"]]
        key = g["gt_rule"]
        if key in shown:
            continue
        shown.add(key)
        r = classify(rec)
        mark = "OK " if r.category == g["ground_truth_category"] else "MISS"
        print(f"[{mark}] {rec['transaction_id']}  {rec['failure_reason']}  ({key})")
        print(f"       pred={r.category}  gt={g['ground_truth_category']}  "
              f"heuristic={r.heuristic_category}  conf={r.confidence:.2f}  [{r.source}]")
        print(f"       {r.reasoning}\n")
