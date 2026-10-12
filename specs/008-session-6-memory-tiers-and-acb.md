# 008 — Session 6: hybrid memory, audience tiers, Actor-Critic-Boss, evals

- **Status:** Implemented
- **Date:** 2026-10-10
- **Scope:** `app/sessions/{models,tier_resolver}.py`, `app/sessions/compression/`,
  `app/services/{estimation,critic,boss}.py`, `app/schemas/{new_estimation_schema,critic,acb}.py`,
  `app/prompts/{estimation/v3,critic/v1,conversation_summary/v1}/`,
  `app/prompts/loader.py`, `app/routers/sessions.py`, `app/config.py`,
  `app/dependencies.py`, `app/services/llm_wrapper.py`, `evals/`, `evals/stress/`,
  `tests/`, `docker-compose.yml`, `pyproject.toml`, `.env.example`, `README.md`,
  `AGENTS.md`
- **Supersedes / Superseded by:** —

## Problem

The reference implementation of the course (`ai-engineering/estimator`) had moved one
phase past ours. Our conversational pipeline stopped at the Session-5 baseline:
a plain sliding window that hard-trimmed the oldest pairs in `append()`, a fixed
audience, no independent review of an estimation, no per-turn telemetry, and no way
to measure the system (no golden set, no stress harness). The gap is catalogued in
`MISSING.md`.

Symptoms of the missing pieces, observable in our code:

1. `ConversationHistory.append()` deleted the oldest pair, so a commitment stated at
   turn 1 (an NDA, a frozen scope, a locked budget) was gone by turn 7 with no trace.
2. Every answer used the same framing regardless of audience; `prompt_version` had no
   `v3`.
3. `EstimationService` had no way to have a draft reviewed, and no structured critic
   contract existed.
4. The conversational response carried no `observation`, so the stress runner the
   course expects could not read telemetry without parsing logs.

## Goals

- Long conversations keep their durable commitments: anchors survive eviction and
  older non-anchor turns collapse into one rolling summary.
- The audience tier is resolved at runtime and drives the prompt.
- An independent Critic can review a draft and a Boss can iterate or synthesize, with
  a full audit trail in the response.
- Each conversational turn emits a structured `TurnObservation` (including real
  token counts and cost).
- A golden-set eval CLI and a stress CLI exist, both hermetic under pytest.

## Non-goals

- Persisting sessions/anchors/summaries across restarts (still module-3 work).
- Wiring `EstimationService.estimate` (the cached non-conversational pipeline) to a
  route — still a spec-007 follow-up.
- Unifying the two pricing tables (spec 001 follow-up).
- An LLM-as-judge metric; the eval metrics stay deterministic (DeepEval remains opt-in
  and unused).
- Rewriting the Streamlit clients to expose the ACB panel.

## Design

**Compression** (`app/sessions/compression/`). `append()` no longer trims; trimming
moved into `CompressionPolicy.apply`, which peels over-cap pairs so it can route each
one to the `AnchorDetector` (heuristic regex by default, `llm` classifier opt-in) or
the `CumulativeSummarizer` (one cheap LLM call, previous summary kept on failure). The
rejected alternative — trimming inside `append`, as before — cannot see the evicted
pair, so anchors are impossible there.

**Tiers** (`app/sessions/tier_resolver.py`). A precedence chain of pure predicates
returns `(tier, rule_name)`; an explicit caller override wins. Prompt v3 renders the
tier into an `<audience>` block and adds an explicit `<output_schema>` contract.
`v2/` is left untouched as the Session-5 baseline.

**ACB** (`app/services/{critic,boss}.py`, `app/schemas/{critic,acb}.py`). The Critic is
a stateless structured call that degrades to a synthetic `accept` on failure so the
pipeline never hard-fails. The Boss is a state machine over `actor → critic → decide`
with `accept | iterate | synthesize`; `synthesize` annotates the last draft with the
open caveats and floors confidence at 30 rather than blanking the answer. The actor is
passed as a callable so the Boss can re-invoke it with the previous feedback.

**Telemetry.** `LLMWrapper.complete_structured_chat` now uses Instructor's
`create_with_completion` to reach the raw provider response and populate
`tokens_in`/`tokens_out`/`cost_usd` in its `meta`, which `estimate_conversational`
folds into `TurnObservation`. (The reference declared the fields but never filled
them; ours does, with a fallback to `create` for older Instructor builds.)

**Evals/stress** (`evals/`, `evals/stress/`). Ported from the reference and adapted to
our schema split: `evals/run.py` drives `/sessions/{id}/estimate` and
`/sessions/{id}/estimate-acb`; the stress runner reads `response.observation` and the
`GET /sessions/{id}` snapshot.

## Contract

```
POST /sessions/{id}/estimate
  transcript, project_type, detail_level, output_format, [tier], [attachments]
→ 200 StructuredEstimationResponse {result, prompt_version:"v3", cache_hit, observation}
     observation = {turn_index, session_id, enriched_transcript_chars,
                    attachments_total_chars, messages_in_window, anchors_count,
                    summary_chars, tokens_in, tokens_out, cost_usd, latency_ms,
                    cache_hit_kind:"none", last_resolved_tier}

POST /sessions/{id}/estimate-acb      (same form fields)
→ 200 ACBResponse {…StructuredEstimationResponse, acb: BossTrace}

GET /sessions/{id}
→ 200 {session_id, message_count, max_turns, metadata,
       anchors_count, summary_chars, last_resolved_tier, last_tier_rule}

POST /sessions/{id}/estimate-acb on an unknown session → 404
input guardrail violation → 400 · upstream failure → 502
```

## Verification

- `uv run pytest -q` → **249 passed** (was 163; +86 across the new modules).
  New files: `test_tier_resolver.py`, `test_compression_anchors.py`,
  `test_compression_policy.py`, `test_critic.py`, `test_boss_orchestration.py`,
  `test_prompts_v3.py`, `test_sessions_acb.py`, `test_evals_metrics.py`,
  `test_evals_run.py`, `test_stress_metrics.py`, `test_stress_runner.py`.
- Updated tests that pinned the old behaviour: `test_sessions_models.py` (append no
  longer trims; `_trim()` and the summary/anchor composition), `test_estimation_service.py`
  (v3, prompt body), `test_sessions_api.py` (v3, fake service signature),
  `test_sessions_integration.py` (composed-context bound, `_trim`).
- Inspection: `python -c "from app.schemas.new_estimation_schema import ACBResponse, TurnObservation"`; the ACB endpoint returns `final_decision`/`iterations_run` in
  `tests/test_sessions_acb.py`.
- Live check: not run against a real provider in this session — the CLIs
  (`python -m evals.run`, `python -m evals.stress.run`) were exercised only through
  their hermetic smoke tests.

## Follow-ups

- Wire `EstimationService.estimate` or delete it (spec 007 follow-up).
- Run the eval + stress CLIs against a live provider and fill `evals/stress/REPORT.md`.
- Consider surfacing the ACB trail and the tier panel in `streamlit_app_loader.py`.
- Add `ruff`/`deepeval`/`fakeredis` dev deps if the course expects them (only `fpdf2`
  was added here, because committed code imports it).
