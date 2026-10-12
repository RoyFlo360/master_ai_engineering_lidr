# AGENTS.md

Agent-facing context for this repository. Read this before changing anything.
`README.md` covers how to run the stack; `specs/` records *why* it is shaped the
way it is. When this file and the code disagree, the code wins — then fix this file.

## What this project is

A FastAPI service that turns a **typed project brief** (or a raw meeting
transcription) into a software project estimation. An exact-match Redis cache sits
in front of the LLM (Cache Augmented Generation), so replaying an identical prompt
costs nothing. Three Streamlit apps are HTTP clients of that service.

This is a learning/course project: one process, no auth, no database, no
multi-tenancy. Prefer small, clear, tested changes over generality. Nothing here
is expected to survive a restart.

## Layout

```
app/main.py                    FastAPI app, CORS, /health, lifespan; mounts both routers
app/config.py                  Settings (pydantic-settings, reads root .env)
app/dependencies.py            lru_cache factories: get_cache, get_llm_wrapper, get_usage_ledger, get_openai_client (None without OPENAI_API_KEY), get_semantic_cache (None unless Redis has RediSearch), get_estimation_service, get_session_store
app/routers/estimations.py     the transcription + typed-v1 HTTP endpoints
app/routers/sessions.py        conversational endpoints: POST /sessions, POST /sessions/{id}/estimate, POST /sessions/{id}/estimate-acb, GET /sessions/{id}
app/schemas/estimation_schema.py      transcription request, TokenUsage, EstimationResponse (legacy shape)
app/schemas/new_estimation_schema.py  typed v1: EstimationRequest/Response, EstimationResult, StructuredEstimationResponse + TurnObservation, ACBResponse
app/schemas/critic.py                 CriticFeedback / CriticIssue — the Critic↔Boss contract
app/schemas/acb.py                    BossTrace / ACBIteration — the Actor-Critic-Boss audit trail
app/schemas/usage_schema.py           UsageTotalsResponse for GET /usage
app/services/llm_service.py    legacy OpenAI-client path behind /estimate (its build_system_prompt also feeds /estimate/stream)
app/services/llm_wrapper.py    LiteLLM path: Router + fallback, cache, cost, usage shapes, Instructor structured calls
app/services/cache.py          EstimationCache (Redis, key = sha256 of prompts + knobs)
app/services/semantic_cache.py vector-similarity cache (redisvl + Redis Stack; no-op without RediSearch)
app/services/estimation.py     the pipeline: estimate() (cached, unwired), estimate_conversational() (tier + compression + observation), estimate_with_acb()
app/services/critic.py         independent audit of an EstimationResult → CriticFeedback (degrades to accept on failure)
app/services/boss.py           stateless Actor→Critic→decide orchestrator (accept | iterate | synthesize)
app/services/usage_ledger.py   process-local running totals
app/sessions/models.py         ConversationHistory (sliding user/assistant window) + ProjectMetadata + Session
app/sessions/store.py          in-memory SessionStore (plain dict, not thread-safe) + SessionNotFoundError
app/sessions/heuristics.py     no-LLM metadata extraction: regexes + curated technology vocabulary
app/sessions/metadata_extractor.py  update_metadata: heuristic first, narrow LLM extractor only when it finds nothing structured
app/sessions/tier_resolver.py  runtime audience tier (executive|pm|developer|default) + the rule that fired
app/sessions/compression/      hybrid memory: anchors.py (never evict), summarizer.py (rolling summary), policy.py (orchestrator)
app/attachments/extractor.py   PDF/DOCX text extraction + transcript enrichment
app/attachments/upload.py      httpx multipart fields for the Streamlit client
app/guardrails/input.py        moderation + prompt-injection + PII checks
app/guardrails/output.py       enforce_scope_response low-confidence filter
app/context/examples.py        reference examples for the LEGACY flows
app/prompts/loader.py          Jinja2 loader; prompts live at <use_case>/<version>/<role>.j2
app/prompts/estimation/v1/     system.j2, user.j2, examples.j2  (the typed v1 flow)
app/prompts/estimation/v2/     system.j2, user.j2  (conversational baseline; adds <project_metadata>)
app/prompts/estimation/v3/     system.j2, user.j2  (v2 + <audience> tier block + <output_schema>; user adds <critic_feedback>)
app/prompts/critic/v1/         the independent-auditor prompts
app/prompts/conversation_summary/v1/  the cumulative summarizer prompts
app/prompts/metadata_extraction/v1/  per-turn metadata extractor prompts
streamlit_app.py               streaming client (SSE) for /estimate/stream
streamlit_app_estimation_api.py  JSON client for /estimate
streamlit_app_loader.py        two-tab client: typed /new-estimate + conversational /sessions — the one compose runs
evals/                         golden-set harness: dataset.py, metrics.py, run.py (CLI), golden_dataset.json (16 cases)
evals/stress/                  Session-6 stress harness: scenarios.py, metrics.py, run.py, analyze.py, fixtures/build_pdfs.py, REPORT.md
tests/                         hermetic pytest suite (260 passing today, no network, no .env); conftest.py + factories.py hold the fakes
specs/                         spec-driven decisions, numbered (001–008 all Implemented); README.md explains the convention
MISSING.md                     gap analysis vs. the reference `ai-engineering/estimator` project
Dockerfile                     python:3.14-slim; uv sync --frozen --no-install-project; one image serves both api and streamlit
docker-compose.yml             redis-stack:7.4.0-v0 + api + streamlit; repo bind-mounted at /srv/app
.streamlit/config.toml         Streamlit runOnSave (Streamlit reads only .streamlit/, never a root config.toml)
src/master_ai_engineering_lidr/__init__.py  uv_build package stub; its console script just prints hello and is NOT installed in the image
```

## The estimation flows — do not confuse them

| Flow | Endpoint(s) | Prompt source | LLM call |
| --- | --- | --- | --- |
| Legacy `/estimate` | `POST /api/v1/estimate` | `llm_service.build_system_prompt()` + `app/context/examples.py`, built in code | `openai` SDK directly (`_call_deepseek`) — **no cache, no fallback, no ledger** |
| Legacy `/estimate/stream` | `POST /api/v1/estimate/stream` | the same `llm_service.build_system_prompt()` | `LLMWrapper.complete_stream()` (LiteLLM Router + fallback + cache) |
| Typed v1 | `POST /api/v1/new-estimate` | `render_estimation_prompt(request, version="v1")` → `app/prompts/estimation/v1/*.j2` | `LLMWrapper.complete()` (LiteLLM Router, fallback, cache) |
| Conversational | `POST /sessions/{id}/estimate` | `render_conversational_prompt(...)` → `app/prompts/estimation/v3/*.j2`; metadata via `app/sessions/heuristics.py` (LLM `metadata_extraction/v1/` only as fallback) | `LLMWrapper.complete_structured_chat()` (Instructor, no cache) |
| Conversational ACB | `POST /sessions/{id}/estimate-acb` | the same v3 prompt, re-rendered with `<critic_feedback>` on each Boss iteration | `LLMWrapper.complete_structured_chat()` for both actor and critic (`app/services/boss.py`) |

The typed and conversational flows use prompt **versioning**; the version is a
string at the call site (`PROMPT_VERSION = "v1"` in `app/routers/estimations.py`,
`conversational_prompt_version = "v3"` in `EstimationService`), so switching
templates is a string change, not a refactor (add `v3/` beside `v2/`).

`docker-compose.yml` runs `streamlit_app_loader.py`, a two-tab client: **Typed
estimation** (`/new-estimate`) and **Conversational session** (`/sessions`). The
other two clients are run by hand.

## Endpoints

- `POST /api/v1/estimate` — `{transcription}` (≥50 chars) → `{estimation, model, provider, usage}`. Upstream failure → **500** (`llm_service` raises `LLMServiceError`, the router maps it to 500, unlike the 502 elsewhere). DeepSeek only.
- `POST /api/v1/estimate/stream` — same body, plus optional `model` / `max_tokens` → SSE: `token` frames, one `usage` frame, `done`; errors as an `error` frame
- `POST /api/v1/new-estimate` — typed body → `{text, prompt_version, cache_hit, usage}`; 422 on a bad field, **502** on upstream failure
- `GET /api/v1/usage` — running totals for this process
- `POST /sessions` — create a session → **201** `{session_id}`
- `POST /sessions/{session_id}/estimate` — multipart form (transcript 20–80k chars + optional PDF/DOCX + optional `tier`) → `StructuredEstimationResponse` (with `observation`); 404 unknown session, 400 input guardrail, 415 unsupported attachment, 422 unreadable attachment / bad form field, 502 upstream
- `POST /sessions/{session_id}/estimate-acb` — same contract, run through the Actor-Critic-Boss loop → `ACBResponse` (structured response + `acb: BossTrace`)
- `GET /sessions/{session_id}` — session metadata + history length + `anchors_count`, `summary_chars`, `last_resolved_tier`, `last_tier_rule`, and the memory itself (`summary` text + `anchor_texts`)
- `GET /health`, `GET /docs`, `GET /redoc`

## Run and verify

```shell
uv sync                                   # .venv from uv.lock
uv run pytest -q                          # the whole suite (fast: ~3s)
uv run uvicorn app.main:app --reload      # API on :8000
uv run streamlit run streamlit_app_loader.py

# Evals / stress (real LLM calls — run against a live API, not in the pytest suite)
uv run python -m evals.run --mode actor   # golden set, in-process; --mode acb for the Boss loop
uv run python -m evals.stress.fixtures.build_pdfs   # build the 5/20/50/100 KB PDFs once
uv run python -m evals.stress.run --http http://localhost:8000   # writes evals/stress/results.csv
uv run python evals/stress/analyze.py             # regenerate the REPORT.md tables from the CSV

docker compose up -d                      # redis + api + streamlit
docker compose exec api pytest -q         # same suite, container's Python
docker compose logs -f api                # structlog output
docker compose restart api
```

A `.env` with at least one provider key is mandatory — `Settings` raises otherwise.

## Conventions

- **Every route declares `response_model=<the Response schema>`.** Declaring the
  *request* model as `response_model` is not a type error, it is a runtime 500 on
  every successful call (spec 004). FastAPI validates the returned object against it.
- **Usage has one shape**: `{input_tokens, output_tokens, total_tokens, cost_usd}`
  (`TokenUsage`). Token counts and cost are always *responses*, never request fields.
- **Free text and structured answers are different schemas.** `EstimationResponse`
  (`{text, prompt_version, cache_hit, usage}`) is the `/new-estimate` free-text reply;
  `StructuredEstimationResponse` (`{result, prompt_version, cache_hit}`) carries the
  validated `EstimationResult`. Never add a required field to one to serve the other
  flow — that is exactly what broke `/new-estimate` (spec 007).
- **Two schema modules reuse the names `EstimationRequest` / `EstimationResponse`.**
  `app/schemas/estimation_schema.py` holds the transcription shapes
  (`EstimationRequest{transcription}`, `EstimationResponse{estimation, model, provider, usage}`,
  `StreamEstimationRequest`, `TokenUsage`); `app/schemas/new_estimation_schema.py` holds
  the typed shapes (`EstimationRequest{description, project_type, detail_level, output_format}`
  and the free-text `EstimationResponse{text, prompt_version, cache_hit, usage}`). The
  router imports the typed pair as `NewEstimationRequest` / `NewEstimationResponse` to
  disambiguate. Check which module a name came from before editing it.
- **Prompt versioning**: templates on disk under `app/prompts/<use_case>/<version>/`,
  rendered by `render_estimation_prompt`; never f-string a prompt together.
- **Schemas in `app/schemas/`, side effects in `app/services/`, HTTP in `app/routers/`.**
- **Tests are hermetic**: `Settings(_env_file=None)`, `openai.OpenAI` replaced by
  `tests/factories.FakeLLM`, and `app.dependency_overrides` for the wrapper and the
  ledger. Never let a test reach a provider or the real cache.
- **Cache-hit costs nothing**: any spend figure must be the *actual* spend, not what a
  cold run would have cost.
- **The conversational pipeline is tier → prompt → LLM → guardrail → append →
  compress → metadata → observation.** Trimming the window is `CompressionPolicy`'s
  job, not `ConversationHistory.append()`'s; anchors and the rolling summary are what
  keep an early commitment alive past the window.
- **The ACB loop persists only the final result.** No matter how many actor drafts the
  Boss runs, the session gets exactly one user/assistant turn (see
  `tests/test_sessions_acb.py`).
- Write a spec in `specs/` for anything larger than a bug fix; see `specs/README.md`.

## Gotchas that have already bitten

1. **`cache_hit` means different things per flow.** `/new-estimate` reports the cached
   token counts with `cost_usd = 0.0` (an identical prompt tokenises identically; the
   replay was not billed). The **streaming** path reports *all zeros*, tokens included
   (`_usage_event(model, None)`). Both are deliberate; don't "fix" one into the other.
2. **The usage ledger is process-local** (`app/services/usage_ledger.py`). It is wiped
   by an API restart *and* by every uvicorn `--reload`, i.e. every code save in dev.
   Only `/new-estimate` records into it. Redis is the upgrade path (spec 006).
3. **`/new-estimate` caps output at 4000 tokens, not `MAX_OUTPUT_TOKENS` (8000).** It
   calls `wrapper.complete()` without `max_tokens`, so the signature default (4000)
   applies. The streaming path uses `Settings.MAX_OUTPUT_TOKENS`. Legacy `/estimate`
   uses a third, hard-coded `MAX_TOKENS = 8000` in `llm_service.py`, independent of
   `Settings`. Three caps, none of them aligned. Unresolved.
4. **Windows/OneDrive bind mounts lag.** A file can be correct on the host and inside
   the container while the *running process* still serves the old module — the reload
   can fire before the content lands. Symptom: `/openapi.json` shows the old schema.
   Fix: `docker compose restart api`. Confirm with
   `docker compose exec -T api sha256sum /srv/app/<path>` against `Get-FileHash` on the host.
5. **Keyword assertions on the system prompt must ignore `<examples>`.** The examples
   block is shared by every variant and its tables contain `confidence_pct` and
   `duration_weeks`, so a whole-prompt assertion passes for the wrong reason. Use
   `instructions_only()` from `tests/test_estimation_v1.py`.
6. **Two pricing tables, two roundings, one product.** `llm_service.cost_llm` keeps its
   own `PRICING` (deepseek-flash only) and rounds to **4** decimals, so
   `POST /estimate` reports `cost_usd: 0.0008`. `llm_wrapper._estimate_cost` keeps
   `MODEL_COSTS` (six models) and rounds to **6**, so streaming and `/new-estimate`
   report `0.000797`. Each has a test asserting its own number, so a change to one looks
   like a local fix and breaks the other. Rounding the wrapper's to 4 was tried and
   reverted on 2026-09-27: at 4 dp any call cheaper than ~$0.00005 reads as `$0.0000`
   and vanishes from the totals. Display precision belongs in the client (`format_cost`
   in `streamlit_app_loader.py`), never in the pricing. Unifying the two tables is
   outstanding — see spec 001's follow-ups.
7. `ESTIMATOR_API_BASE_URL` is read by the Streamlit apps directly via `getenv` — it is
   **not** part of `Settings`. Compose sets it to `http://api:8000`; it defaults to
   `http://localhost:8000`.
8. `EstimationCache.make_key` hashes the system prompt, user message and generation
   knobs into `estimation:v2:<sha256>`. Changing `thinking_budget`, `max_tokens`, the
   model or any template text silently invalidates every entry — which is the intended
   behaviour, not a bug. Bump the `v2` prefix only when the *stored payload shape* changes.
9. **Two surfaces have no automated coverage**: `EstimationCache` — the suite never
   reaches Redis, and the graceful-degradation path has never been exercised at all —
   and `LLMWrapper.complete_stream`, since the endpoint tests inject a fake wrapper.
   Both are written down in specs 003 and 002. Change either and you verify by hand.
10. **Sessions are process-local** (`app/dependencies.get_session_store`, an
    `lru_cache`d `SessionStore`). An API restart wipes them and a second uvicorn worker
    forks them. Only `/sessions/*` touches the store.
11. **The semantic cache needs Redis Stack, not vanilla `redis:7-alpine`.**
    `EstimationSemanticCache` checks `module_list()` for RediSearch and sets
    `available = False` when it is absent (or Redis is unreachable), turning
    `lookup`/`store` into no-ops. The dependency does that check **before** building
    the OpenAI vectorizer (which probes the embedding model at construction), so a
    vanilla Redis fails fast instead of blocking the first request on the network.
    Compose ships `redis/redis-stack:7.4.0-v0`, so RediSearch is present and the
    semantic cache *can* activate — but only when `OPENAI_API_KEY` is set (the
    vectorizer needs embeddings). Without that key `get_semantic_cache` returns None
    and the cache stays a no-op. The unwired `EstimationService.estimate` is the only
    caller, so no live endpoint uses it yet.
12. **`EstimationService.estimate` is not wired to a route.** `/new-estimate` calls
    `LLMWrapper.complete()` directly, so the Session-4 cached pipeline is only reached
    from tests. Wire it or delete it — spec 007 lists it as a follow-up.
13. **Metadata refresh is a heuristic first, an LLM call only as fallback.**
    `app/sessions/heuristics.py` extracts the project name/team/tech/scope with no
    billed call; the narrow LLM extractor in `app/sessions/metadata_extractor.py`
    runs only when the heuristic finds no structured facts for the turn (disable with
    `METADATA_LLM_FALLBACK=false`). The system prompt is never stored in history —
    `ConversationHistory.to_messages(system_prompt=…)` regenerates and prepends it.
14. **DeepSeek structured calls must force thinking off.** Instructor sends
    `tool_choice`, and DeepSeek rejects it while thinking is on ("Thinking mode does
    not support this tool_choice"). `_thinking_off_extra_body` centralises the
    switch; both the text (`_build_call_kwargs`) and structured
    (`complete_structured_chat`) paths use it. Forgetting it on a new call path is a
    502 with `InstructorRetryException`.
15. **One API key per provider; model overrides use their own.** `_api_key_for`
    routes `gpt-*`/`o1`/`o3` to `OPENAI_API_KEY`, `claude*` to `ANTHROPIC_API_KEY`,
    and `deepseek*` to `DEEPSEEK_API_KEY` (falling back to the OpenAI slot). Sending
    the DeepSeek key to an OpenAI model is an auth error, not a fallback — the
    `gpt-4o-mini` metadata extractor did exactly that once.
16. **Legacy `/estimate` never touches Redis, the ledger or the fallback.** Only
    `/estimate/stream` and `/new-estimate` go through `LLMWrapper` (hence
    `EstimationCache` and the usage ledger). `llm_service.generate_estimation` calls
    DeepSeek directly, so posting the same transcription to `/estimate` twice is billed
    twice, records nothing in `GET /api/v1/usage`, and gets no provider fallback. Any
    spend or cache-hit claim is about the other two flows, never `/estimate`.
17. **Two cache-key builders both emit `estimation:v2:<sha256>`.**
    `EstimationCache.make_key` hashes the rendered system prompt + user message +
    generation knobs (both wrapper paths); `_exact_cache_key` in
    `app/services/estimation.py` hashes the typed request fields + `prompt_version` +
    model (the unwired Session-4 service). The payloads differ, so digests never
    collide — but `/new-estimate` and `EstimationService.estimate` will NOT share a
    cache entry for the same typed request, and the shared `v2` prefix is bumped in two
    places' worth of meaning.
18. **The legacy prompt is in EUR but its examples are in USD.**
    `llm_service.build_system_prompt` says "developer rate of approximately 62.50
    EUR/hour" and "Total cost in EUR", yet every reference table in
    `app/context/examples.py` is headed "Cost (USD)" with six-figure USD totals. The
    typed/conversational examples (`app/prompts/estimation/v1/examples.j2`) use
    `cost_eur`. Unresolved — fix the legacy examples or drop the currency claim.
19. **`llm_wrapper.py`'s module docstring is stale.** It claims "the blocking
    `complete()` path was removed as dead code", but `LLMWrapper.complete()` is exactly
    what `/new-estimate` calls (and `complete_stream()` is what the streaming route
    calls). Trust the code, not that paragraph.
20. **Input moderation fails open.** `_check_moderation` in `app/guardrails/input.py`
    catches any exception (network, auth, rate limit) and returns after logging
    `moderation_call_failed`; only the regex layers (prompt injection, PII) are
    unconditional. With no `OPENAI_API_KEY` — or during an OpenAI outage — moderation is
    silently skipped, not a 400. Deliberate (a broken moderation call must not block
    estimations), but do not claim the guardrail always runs.
21. **The semantic-cache index pins `dims: 1536`.** `_INDEX_SCHEMA` in
    `app/services/semantic_cache.py` hard-codes 1536 (`text-embedding-3-small`) and
    never derives it from `EMBEDDING_MODEL`; pointing that setting at a different-width
    model makes the index unusable. This is now live on the compose stack (Redis
    Stack + an `OPENAI_API_KEY`), not merely a hypothetical.
22. **`ConversationHistory.append()` does NOT trim.** Trimming moved into
    `CompressionPolicy` so it can see — and route — the evicted pairs. Calling
    `append` in a loop grows `messages` without bound until `apply_compression` runs;
    `_trim()` still exists as the plain-window helper for callers that only want the
    invariant. `tests/test_sessions_models.py` pins both behaviours.
23. **The conversational path gets one turn per request, even under ACB.**
    `estimate_with_acb` appends only the Boss-approved draft; the intermediate actor
    drafts never touch the session. A test asserts exactly one user/assistant pair.
24. **`TurnObservation.tokens_in/out` and `cost_usd` are real here.**
    `LLMWrapper.complete_structured_chat` uses Instructor's
    `create_with_completion` to reach the raw provider response and read its `usage`
    (falling back to `create` on an older Instructor). The reference project left these
    at zero; ours does not. Fakes in tests must still answer by `response_model`
    (including the summarizer's private `_SummaryEnvelope`).
25. **The eval and stress harnesses are CLIs, not pytest.**
    `uv run python -m evals.run --mode actor|acb [--limit N] [--http URL]` scores the
    golden set; `uv run python -m evals.stress.run --http …` writes `results.csv`.
    `evals/stress/fixtures/build_pdfs.py` needs `fpdf2` (a declared dependency) and
    must be run once before a size > 0 stress run. Their artifacts are gitignored.
26. **`MAX_ATTACHMENT_CHARS` is 60_000** (raised from 20_000 in spec 009) so the
    stress corpus's 50 KB point sits just under the cap and its 100 KB point
    truncates. The extracted-text cap is what the size curve actually measures.
27. **Auxiliary LLM calls fall back to `AUX_FALLBACK_MODEL`.** The metadata
    extractor, summariser, critic and anchor classifier default to an OpenAI model;
    when that provider fails (no credits / quota), `complete_structured_chat` — but
    only for calls that pass `model_override` — retries once on the fallback
    (default `deepseek/deepseek-flash`). The main estimation call passes no override
    and is never re-routed. Without a working aux provider the summary stays empty and
    the critic returns `accept` at confidence 0, silently.
28. **`GET /sessions/{id}` returns the memory text, not just its size.** `summary`
    and `anchor_texts` were added so the stress exercise's `MemoryDriftMetric` can
    search what the session actually remembers. It is a debug endpoint, so the full
    text is acceptable.
