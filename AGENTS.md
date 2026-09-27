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
app/main.py                    FastAPI app, CORS, /health, lifespan
app/config.py                  Settings (pydantic-settings, reads root .env)
app/dependencies.py            lru_cache factories: get_cache, get_llm_wrapper, get_usage_ledger
app/routers/estimations.py     every HTTP endpoint lives here
app/schemas/estimation_schema.py      transcription request, TokenUsage, EstimationResponse
app/schemas/new_estimation_schema.py  typed v1 request/response (EstimationRequest/Response)
app/schemas/usage_schema.py           UsageTotalsResponse for GET /usage
app/services/llm_service.py    legacy OpenAI-client path behind /estimate + /estimate/stream
app/services/llm_wrapper.py    LiteLLM path: Router + fallback, cache, cost, usage shapes
app/services/cache.py          EstimationCache (Redis, key = sha256 of prompts + knobs)
app/services/usage_ledger.py   process-local running totals
app/context/examples.py        reference examples for the LEGACY flows
app/prompts/loader.py          Jinja2 loader; prompts live at <use_case>/<version>/<role>.j2
app/prompts/estimation/v1/     system.j2, user.j2, examples.j2  (the typed v1 flow)
streamlit_app.py               streaming client (SSE) for /estimate/stream
streamlit_app_estimation_api.py  JSON client for /estimate
streamlit_app_loader.py        form client for /new-estimate — the one compose runs
tests/                         hermetic pytest suite (no network, no .env)
specs/                         spec-driven decisions, numbered
```

## The two estimation flows — do not confuse them

| Flow | Endpoint(s) | Prompt source | LLM call |
| --- | --- | --- | --- |
| Legacy | `POST /api/v1/estimate`, `POST /api/v1/estimate/stream` | `app/services/llm_service.py` + `app/context/examples.py`, built in code | `openai` client directly |
| Typed v1 | `POST /api/v1/new-estimate` | `render_estimation_prompt(request, version="v1")` → `app/prompts/estimation/v1/*.j2` | `LLMWrapper.complete()` (LiteLLM Router, fallback, cache) |

Only the typed flow uses prompt **versioning**; the version is the string
`PROMPT_VERSION` in `app/routers/estimations.py`, so switching templates is a
string change, not a refactor (add `v2/` beside `v1/`).

`docker-compose.yml` runs `streamlit_app_loader.py`, so the typed flow is the one
you see in the browser; the other two clients are run by hand.

## Endpoints

- `POST /api/v1/estimate` — `{transcription}` (≥50 chars) → `{estimation, model, provider, usage}`
- `POST /api/v1/estimate/stream` — same body → SSE: `token` frames, one `usage` frame, `done`; errors as an `error` frame
- `POST /api/v1/new-estimate` — typed body → `{text, prompt_version, cache_hit, usage}`
- `GET /api/v1/usage` — running totals for this process
- `GET /health`, `GET /docs`

## Run and verify

```shell
uv sync                                   # .venv from uv.lock
uv run pytest -q                          # the whole suite (fast: ~1s)
uv run uvicorn app.main:app --reload      # API on :8000
uv run streamlit run streamlit_app_loader.py

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
- **Prompt versioning**: templates on disk under `app/prompts/<use_case>/<version>/`,
  rendered by `render_estimation_prompt`; never f-string a prompt together.
- **Schemas in `app/schemas/`, side effects in `app/services/`, HTTP in `app/routers/`.**
- **Tests are hermetic**: `Settings(_env_file=None)`, `openai.OpenAI` replaced by
  `tests/factories.FakeLLM`, and `app.dependency_overrides` for the wrapper and the
  ledger. Never let a test reach a provider or the real cache.
- **Cache-hit costs nothing**: any spend figure must be the *actual* spend, not what a
  cold run would have cost.
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
   applies. The streaming path uses `Settings.MAX_OUTPUT_TOKENS`. Unresolved.
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
