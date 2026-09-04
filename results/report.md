# Payment Failure Recovery Agent - Batch Report

- **Run:** 2026-08-27T12:03:21
- **Classifier backend:** `offline` (offline-heuristic)
- **Records processed:** 200
- **Guardrails:** max 3 retries, 4h cooldown

## Headline

| Metric | Value |
|---|---|
| Recovery rate (count) | **71.5%** (143/200) |
| Amount at risk | Rs 6,246,101 |
| Amount recovered (simulated) | **Rs 4,910,758** |
| Value recovery rate | 78.6% |

## Outcomes

| Outcome | Count |
|---|---|
| recovered | 143 |
| still_failed | 21 |
| escalated | 19 |
| no_action_taken | 17 |

## Decision-to-retry accuracy

Scored against `ground_truth_recoverable` - did the agent retry the
things a retry could actually recover, and leave the rest alone?

| | Value |
|---|---|
| Precision | **71.4%** |
| Recall | **100.0%** |
| F1 | 83.3% |
| TP / FP / FN / TN | 90 / 36 / 0 / 74 |

**Best-action agreement:** 150/200 (75.0%) - agent's first action matched `ground_truth_best_action`.

## Classifier quality - context-aware LLM vs. flat lookup

Category accuracy against `ground_truth_category`. The dataset is ~25% context-dependent cases where `failure_reason` alone is misleading.

| | LLM | flat heuristic |
|---|---|---|
| Overall | **75.0%** | 75.0% |
| On clean records | 100.0% | 100.0% |
| On twist records (50) | **0.0%** | 0.0% |

LLM / heuristic disagreed on **0** records (0.0%).

## Hard-decline restraint

- Transactions that must be left alone (ground truth): **9**
- Correctly left alone by the agent: **9** (100.0%)
- Hard declines wrongly retried: **0**

## False-positive cost

- Transactions where a retry was spent on a non-recoverable failure: **36**
- Total wasted retry attempts: **46**
- Estimated cost: **Rs 322.00** (@ Rs 7.0/attempt)

## Recovery rate by failure reason

| failure_reason | recovered / total | rate | Rs recovered |
|---|---|---|---|
| bank_timeout | 30/38 | 79% | Rs 968,808 |
| card_blocked | 0/4 | 0% | Rs 0 |
| card_expired | 3/12 | 25% | Rs 15,985 |
| card_reported_stolen | 0/2 | 0% | Rs 0 |
| expired_mandate | 15/22 | 68% | Rs 536,130 |
| insufficient_funds | 56/60 | 93% | Rs 1,447,825 |
| invalid_otp | 6/10 | 60% | Rs 478,135 |
| issuer_declined_fraud_suspected | 0/11 | 0% | Rs 0 |
| network_error | 28/28 | 100% | Rs 1,429,462 |
| wrong_cvv | 5/13 | 38% | Rs 34,414 |

## Recovery rate by category (classifier)

| category | recovered / total | rate |
|---|---|---|
| hard_decline | 0/17 | 0% |
| needs_customer_action | 14/35 | 40% |
| needs_reauth | 15/22 | 68% |
| soft_recoverable | 114/126 | 90% |

## Guardrail activity (rule fires across all decisions)

| rule | fires |
|---|---|
| `route:soft_recoverable` | 126 |
| `cooldown:4h_floor` | 126 |
| `timing:salary_credit_window` | 60 |
| `route:needs_customer_action` | 35 |
| `route:needs_reauth` | 22 |
| `guardrail:hard_decline_denylist` | 17 |
| `guardrail:retry_cap_reached` | 12 |
| `followup:reauth_not_completed` | 7 |

_Classification sources: {'fallback': 200}_
