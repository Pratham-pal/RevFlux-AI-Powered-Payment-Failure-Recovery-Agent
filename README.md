# Payment Failure Recovery Agent

**Razorpay AI Buildathon 2026 — Track 3: AI Revenue Recovery**

An AI system that looks at failed payment events, reasons about *why* each one
failed, and chooses the right recovery action — instead of blindly retrying
every failure. It knows when **not** to act: a blocked card or a fraud-suspected
decline is left alone, an expired mandate triggers a re-authorization flow, a
wrong CVV triggers a customer nudge, and only genuinely transient failures get a
retry — scheduled intelligently, capped at 3 attempts, cooldown-enforced, and
escalated to a human when the cap is hit.

The demo site takes a **real Razorpay test-mode payment**, lets it fail, and
animates the failed transaction travelling through the recovery pipeline —
classifier → policy engine → executor → audit — with a live **naive-retry vs.
agent** comparison.

---

## Quick start

```powershell
# from the project root
pip install -r requirements.txt

python data\generate_dataset.py     # -> data/failed_payments.json, eval/ground_truth.json
python main.py                       # -> results/  (summary.json, report.md, audit_log.jsonl)

python -m uvicorn web.app:app --reload         # open http://localhost:8000
```

Everything runs with **no API key and no Razorpay account** — the classifier
falls back to an offline heuristic and the checkout falls back to a local mock.
Add credentials (below) to make it real.

---

## Architecture

```
data/generate_dataset.py     200 synthetic failed payments + hidden ground truth
        │
        ▼
agent/classifier.py          LLM JUDGMENT  — which of 5 recovery categories?
        │                    (Ollama local / Claude API / offline heuristic)
        ▼
agent/policy_engine.py       DETERMINISTIC GUARDRAILS — concrete action + timing
        │                    retry cap · cooldown · hard-decline denylist · escalation
        ▼
agent/executor.py            SIMULATED action (no real charge) + bounded retry loop
        │
        ▼
agent/audit_log.py           every decision → results/audit_log.jsonl
        │
        ├─ main.py           batch over 200 + score vs ground truth → results/
        └─ web/app.py        FastAPI demo site (live single-transaction + batch view)
```

### The safety design — LLM judgment vs. deterministic guardrails

| | `agent/classifier.py` | `agent/policy_engine.py` |
|---|---|---|
| Powered by | an LLM | plain Python — no LLM, no randomness |
| Decides | *what kind* of failure this is (1 of 5 categories) + reasoning | the concrete action, retry delay, whether to stop |
| Can it cause a retry? | **No** — its output has no "action" field | Yes — and only within hard limits |

Hard limits, all enforced in deterministic code:

- **Hard-decline denylist** — `card_blocked`, `issuer_declined_fraud_suspected`,
  `card_reported_stolen` are blocked in Python **regardless of the classifier's
  output**. A disagreement is logged, not obeyed.
- **Confidence gate** — a classification below **0.6** confidence is escalated
  to a human, not acted on.
- **Retry cap** — never more than **3** attempts total across a charge's lifetime.
- **Cooldown** — never a retry within **4 hours** of the previous attempt.
- **Escalation** — cap hit, low confidence, `needs_review`, or an unrecognized
  category → `escalate_manual_review`.

The retry loop lives in `executor.py`, but the *decision to stop* lives in
`policy_engine.py` — the loop is bounded by the cap by construction.

### Categories → actions

| Category (LLM) | Failure reasons | Action (policy engine) |
|---|---|---|
| `hard_decline` | card blocked, fraud-suspected decline, card stolen | `no_action` |
| `soft_recoverable` | insufficient funds, bank timeout, network error | `smart_retry` (timed) |
| `needs_reauth` | expired UPI mandate | `request_mandate_reauth` |
| `needs_customer_action` | wrong CVV, expired card, invalid OTP, timeout-on-an-expired-card | `nudge_customer` |
| `needs_review` | chronic repeat failures, likely-false fraud flag, non-transient "transient" errors, data mismatches | `escalate_manual_review` |

**Smart-retry timing:** transient failures retry after a 4–6h delay;
`insufficient_funds` retries 24–48h out and is *snapped onto the 1st–5th of the
month* (salary-credit window) when one falls inside the 24–72h window.

### How accuracy is measured

The synthetic dataset is **~25% context-dependent** — cases where the raw
`failure_reason` is misleading and only the context (instrument status, repeat-
failure history, prior retries, transaction type) reveals the right action. A
flat `failure_reason → category` lookup gets all of these wrong.

```
python eval/run_eval.py          # confusion matrix, per-category P/R/F1,
                                 # LLM vs. heuristic, clean vs. context split
```

`agent/ground_truth.py` is the context-aware oracle the classifier is scored
against (the classifier never imports it). `eval/history.jsonl` tracks accuracy
by `prompt_version` so prompt changes are measurable.

---

## Classifier backends

Selected with `RECOVERY_AGENT_BACKEND` (`auto` | `ollama` | `claude` | `offline`):

| Backend | Cost | Notes |
|---|---|---|
| `ollama` | free | Local model via [Ollama](https://ollama.com). Real reasoning, no network. |
| `claude` | ~₹1–2 per demo | Anthropic API. Needs `ANTHROPIC_API_KEY`. Best quality. |
| `offline` | free | Flat `failure_reason → category` lookup. No reasoning — a safety net. |
| `auto` *(default)* | — | Ollama → Claude → offline, whatever is reachable. |

Any runtime failure degrades to `offline` so a run always completes.

```powershell
# local, free, real reasoning:
ollama pull llama3.1:8b
python -m uvicorn web.app:app --reload

# real Claude for the live demo:
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:RECOVERY_AGENT_BACKEND = "claude"
$env:CLASSIFIER_MODEL = "claude-sonnet-5"
```

---

## Razorpay test mode

1. Create a free account at [razorpay.com](https://razorpay.com), stay in **Test Mode**.
2. **Settings → API Keys → Generate Test Key** → you get `rzp_test_...` + a secret.
3. Copy `.env.example` to `.env` and fill in:
   ```
   RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxx
   RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
   ```
4. Restart the server. The checkout badge flips to "Razorpay test mode" and the
   Pay button opens a real Razorpay Checkout. Choose **Failure** (or a failing
   test card) to trigger the agent.

Without keys, the site uses a built-in mock that produces the same failure-event
shape — the pipeline demo is identical.

---

## Deploy (Render)

1. `git init && git add . && git commit -m "..."`, push to GitHub.
2. Render → **New → Blueprint** → pick the repo (`render.yaml` is already here).
3. In the Render dashboard, set the secret env vars: `ANTHROPIC_API_KEY` (optional),
   `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
4. `results/` is committed, so the Batch tab works immediately. Regenerate it
   locally with a real backend (`ollama` / `claude`) before pushing if you want
   real reasoning strings in the batch audit trail.

> Free tier sleeps after ~15 min idle (cold start ~30–60s). Hit the URL a few
> minutes before presenting.

---

## What gets measured (`main.py`)

Scored against `eval/ground_truth.json` (the agent never sees it), reporting
**two separate accuracy views**:

1. **Decision-to-retry precision / recall** — vs `ground_truth_recoverable`. Did
   the agent retry what a retry could actually recover, and leave the rest alone?
   A wrongly-retried hard decline shows up here as a false positive.
2. **Best-action agreement** — vs `ground_truth_best_action` (5-way). Did the
   agent pick the right *kind* of intervention, including the re-auth and nudge
   paths that are legitimate recoveries but not blind retries?

Plus: overall recovery rate, recovery rate per failure reason, hard-decline
restraint, false-positive cost (wasted retries × blended per-retry cost), and
total simulated ₹ recovered.

### Simulated outcome model (no real payment calls)

`P(attempt succeeds) = INTERVENTION_EFFECTIVENESS[action][category]`. The agent
always pairs the fitting action with the category (the diagonal); a naive "retry
everything" baseline hits the off-diagonal and mostly fails:

| Action ↓ / Category → | soft_recoverable | needs_reauth | needs_customer_action | hard_decline |
|---|---|---|---|---|
| smart_retry | **0.60** | 0.03 | 0.03 | 0.0 |
| request_mandate_reauth | 0.10 | **0.70** | 0.10 | 0.0 |
| nudge_customer | 0.10 | 0.15 | **0.40** | 0.0 |

---

## Project layout

```
data/generate_dataset.py     synthetic data generator (seeded, reproducible)
eval/ground_truth.json        hidden labels, keyed by transaction_id
agent/config.py               every tunable in one place
agent/classifier.py           stage 1 — LLM judgment (3 backends)
agent/policy_engine.py        stage 2 — deterministic guardrails
agent/executor.py             stage 3 — simulated execution + retry loop
agent/naive.py                the "retry everything" baseline for comparison
agent/audit_log.py            stage 4 — JSON-lines audit trail
main.py                       batch runner + scoring
web/app.py                    FastAPI: /api/config, /api/create-order, /api/recover, /api/batch
web/recovery_service.py       bridges agent/ to the web layer
web/razorpay_client.py        Razorpay test-mode order creation
web/static/                   index.html + app.js (the demo site)
scripts/preview_decisions.py  classify+decide dry run over all 200
results/                      generated: summary.json, report.md, audit_log.jsonl, transactions.json
render.yaml / Procfile        deploy config
```

## Requirements

Python 3.10+, `anthropic`, `fastapi`, `uvicorn`, `razorpay`, `python-dotenv`.
The Ollama backend uses the standard library only. See `requirements.txt`.
