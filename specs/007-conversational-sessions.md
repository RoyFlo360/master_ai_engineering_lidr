# 007 — Conversational sessions, attachments and the structured response split

- **Status:** Implemented
- **Date:** 2026-10-05
- **Scope:** `app/routers/sessions.py`, `app/schemas/new_estimation_schema.py`,
  `app/services/estimation.py`, `app/services/llm_wrapper.py`,
  `app/services/semantic_cache.py`, `app/config.py`, `app/main.py`,
  `app/sessions/`, `app/attachments/`, `app/guardrails/`,
  `app/prompts/estimation/v2/`, `app/prompts/metadata_extraction/`, `tests/`
- **Supersedes / Superseded by:** —

## Problem

Session 5 added a multi-turn estimator: an in-memory session store, PDF/DOCX
attachment extraction, input/output guardrails, a semantic cache, and three
`/sessions` endpoints. As merged it did not run:

1. **`EstimationResponse` was overloaded.** A `result: EstimationResult` field was
   added to the *free-text* response of `POST /api/v1/new-estimate`. That field is
   required, so the endpoint — which returns `text`, `prompt_version`, `cache_hit`
   and `usage` — failed validation and every success became a 500. Evidence: 14
   failures in `tests/test_estimation_v1.py`, e.g.
   `ValidationError: 1 validation error for EstimationResponse / result / Field required`.
2. **The router was never mounted.** `app/main.py` included `estimations.router`
   only, so `/sessions` was unreachable even from `/openapi.json`.
3. **Nothing rendered.** `render_conversational_prompt` asked for
   `estimation/v2/*.j2` and `render_metadata_extraction_prompt` for
   `metadata_extraction/v1/*.j2`; neither directory existed, so every
   conversational call would raise `TemplateNotFound`.
4. **`LLMWrapper.complete_structured` did not exist**, yet
   `EstimationService.estimate` called it, and the Session-5 settings
   (`MAX_ATTACHMENT_CHARS`, `EMBEDDING_MODEL`, …) were missing from `Settings`,
   so `get_estimation_service` and the attachment loop raised `AttributeError`.

Diagnosis: one schema was doing two jobs, and the feature was half-wired.

## Goals

- The free-text `/new-estimate` contract is exactly
  `{text, prompt_version, cache_hit, usage}` again, and its route tests are green.
- A dedicated `StructuredEstimationResponse`
  (`{result, prompt_version, cache_hit}`) serves the conversational and
  Session-4 service pipelines.
- `/sessions`, `/sessions/{id}` and `/sessions/{id}/estimate` are mounted and
  appear in `/openapi.json`.
- The conversational endpoint runs end to end with real prompt templates.
- A Redis without RediSearch (vanilla `redis:7-alpine`, which `docker-compose.yml`
  ships) disables the semantic cache instead of failing requests.
- Unit and integration coverage for the new surface.

## Non-goals

- **No session persistence.** Sessions stay in process memory; a restart and a
  second uvicorn worker still lose or fork state. Module 3 territory.
- **No session UI.** `docker compose` keeps running the `new-estimate` form;
  the sessions endpoints are exercised through `/docs` or the tests.
- **No semantic cache on vanilla Redis.** It is documented as requiring Redis
  Stack; the compose stack keeps vanilla Redis and therefore runs without it.
- **No unification of the two pricing tables** (spec 005's outstanding item).

## Design

### Two response schemas, two jobs

`EstimationResponse` keeps its original free-text shape. The structured answer
gets its own model, `StructuredEstimationResponse`, in the same module. The
split is the fix for (1): a free-text answer has no phases to validate, and a
validated answer has no rendered `text` or billed `usage` to report. Reusing one
model meant the required fields of one flow broke the other.

`EstimationService.estimate` and `EstimationService.estimate_conversational` both
return `StructuredEstimationResponse`; the `/new-estimate` router is untouched.

### Wiring the conversational path

- `app/main.py` now includes `sessions.router`.
- `Settings` gains the Session-5 knobs (`MAX_ATTACHMENT_CHARS`,
  `MAX_CONVERSATION_TURNS`, `METADATA_EXTRACTOR_MODEL`, `METADATA_LLM_FALLBACK`,
  `EMBEDDING_MODEL`, `SEMANTIC_CACHE_THRESHOLD`, `SEMANTIC_CACHE_TTL`,
  `SEMANTIC_CACHE_LOG_ONLY`).
- `LLMWrapper.complete_structured` builds a two-message array and delegates to
  the existing `complete_structured_chat`; the shared credential lookup moves to
  `_api_key_for`, which routes each model to its own provider's key (OpenAI,
  Anthropic, DeepSeek — the last keeping the DEEPSEEK-then-OPENAI fallback). Both
  paths send `_thinking_off_extra_body`, because Instructor's `tool_choice` is
  rejected by DeepSeek while thinking is on ("Thinking mode does not support this
  tool_choice").
- `app/prompts/estimation/v2/system.j2` adds a `<project_metadata>` block to the
  v1 instructions and reuses the v1 `<examples>` calibration block. The block is
  empty on the first turn and carries the facts agreed so far afterwards.

### Metadata: heuristic primary, LLM fallback

`app/sessions/heuristics.py` refreshes `ProjectMetadata` after every turn with
no extra billed call: regexes for the project name and team size, a curated
(and ambiguity-aware) technology vocabulary over the transcript and the
estimation text, and `agreed_scope` set to the latest non-empty estimation
summary. The heuristic was chosen over an LLM-only extractor because it is cheap,
synchronous and deterministic — and therefore easy to test.

`app/sessions/metadata_extractor.py` keeps the narrow LLM extractor
(`metadata_extraction/v1/*.j2`) as a **fallback**: it only runs when the
heuristic found no project name, team size or technology for the turn, and
`METADATA_LLM_FALLBACK` is true. Both paths merge with `ProjectMetadata.merge_with`
(scalar overwrite, technology union) and swallow their own failures.

### The sliding window and the system prompt

`ConversationHistory` keeps the last `MAX_CONVERSATION_TURNS` pairs and drops the
oldest. The system prompt is never stored — it is regenerated from the current
metadata each turn and prepended by `ConversationHistory.to_messages(system_prompt=…)`,
so it is always present and always current. The method keeps its existing name and
returns the bounded history alone when called without a prompt.

### Semantic cache degradation

`EstimationSemanticCache` checks for the RediSearch module once at
construction (`module_list()`), and again after `SearchIndex.create`. If either
fails, `available` is `False` and `lookup`/`store` become no-ops, logging
`semantic_cache_unavailable`. The dependency (`get_semantic_cache`) runs the
module check **before** constructing the OpenAI vectorizer, because the
vectorizer probes the embedding model at construction — on vanilla Redis that
probe blocked the first request for ~40s for a cache that could never serve it.
This is the same graceful-degradation contract the exact-match cache already had
(spec 003), and it is what lets the compose stack run on vanilla
`redis:7-alpine`.

## Contract

```
POST /sessions                         → 201 {"session_id": "<uuid>"}
GET  /sessions/{session_id}            → 200 {session_id, message_count, max_turns, metadata}
GET  /sessions/{session_id}            → 404 on an unknown id

POST /sessions/{session_id}/estimate   multipart/form-data
  transcript      text   (20..80_000 chars, required)
  project_type    enum   required
  detail_level    enum   required
  output_format   enum   required
  attachments     file[] optional, .pdf or .docx
→ 200 {"result": {…EstimationResult…}, "prompt_version": "v2", "cache_hit": false}
→ 400 {"reason": "prompt_injection"|"pii"|"moderation", "message": "…"}   (input guardrail)
→ 404 {"detail": "session_not_found"}
→ 415 {"reason": "unsupported_attachment", "filename": "…"}
→ 422 {"reason": "attachment_extraction_failed", "filename": "…", "message": "…"}
→ 502 {"detail": "Upstream LLM call failed"}
```

## Verification

- `uv run pytest -q` → `163 passed` (was 54 passed / 14 failed before the fix).
  New files: `tests/test_estimation_schema.py`, `tests/test_sessions_models.py`,
  `tests/test_attachments.py`, `tests/test_guardrails.py`,
  `tests/test_semantic_cache.py`, `tests/test_estimation_service.py`,
  `tests/test_sessions_api.py`, `tests/test_sessions_heuristics.py`,
  `tests/test_sessions_integration.py`.
- Regression tests for the issues manual testing surfaced after the first cut:
  `tests/test_attachments.py` pins the httpx multipart list shape that
  `app/attachments/upload.py` builds; `tests/test_llm_wrapper.py` pins the
  per-provider key routing and the DeepSeek thinking switch on the structured
  path; `tests/test_semantic_cache.py` pins the RediSearch-before-embedding-client
  ordering.
- `tests/test_sessions_integration.py` drives the real app over
  `httpx.AsyncClient` + `ASGITransport` and covers the six Session-5 scenarios:
  two-turn metadata accumulation; a PDF that changes the result; eight turns that
  never send more than `MAX_TURNS`; empty values; weird (emoji/kanji/symbol)
  values; and a performance smoke (a loose wall-clock bound plus a deterministic
  bound on the window size).
- `tests/test_estimation_schema.py::test_estimation_response_has_exactly_the_v1_fields`
  pins the free-text shape; `tests/test_sessions_api.py::test_the_openapi_declares_the_structured_response_model`
  pins the structured one.
- `/openapi.json` lists `/sessions`, `/sessions/{session_id}` and
  `/sessions/{session_id}/estimate`; the estimate `200` schema is
  `StructuredEstimationResponse`.
- Prompt rendering checked directly: the v2 system prompt contains
  `<project_metadata>` (empty when `metadata_is_empty`) and `<examples>`, and the
  LLM fallback prompts render with an empty and a populated `ProjectMetadata`.
- `docker compose config` validates (see README's Docker section) and the suite
  runs inside the `api` container.

Not verified: a live end-to-end conversational estimate against a real provider
and a Redis Stack semantic hit — both need credentials/Stack and are exercised by
hand. `EstimationService.estimate` remains unwired to a route.

## Follow-ups

- Wire `EstimationService.estimate` into `/new-estimate` (or delete it) — it is
  currently only reachable from tests.
- Move the session store to Redis before running more than one worker.
- Provide a Redis Stack service in compose if the semantic cache is meant to be
  demonstrable out of the box.
