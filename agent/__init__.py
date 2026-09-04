"""Payment Failure Recovery Agent.

Pipeline:  classifier (LLM judgment)  ->  policy_engine (deterministic guardrails)
           ->  executor (simulated action)  ->  audit_log (compliance trail)

The split between `classifier` and `policy_engine` is deliberate and is the
core safety design of this project: the LLM is only ever asked *what kind of
failure is this*, and every hard limit (retry caps, hard-decline exclusions,
cooldowns, escalation) lives in plain deterministic Python where a model
hallucination cannot reach it.
"""
