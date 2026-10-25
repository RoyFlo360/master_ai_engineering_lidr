# Architecture

## Context

One FastAPI application turns a transcription or a typed brief into a software
project estimation. It is a course project: one process, no authentication, no
database, no multi-tenancy. Three Streamlit apps are plain HTTP clients of the API.

Redis is optional at runtime. When it is unreachable the exact-match cache logs a
warning and the request proceeds uncached; when it lacks RediSearch the semantic
cache stays a no-op. The API does not depend on Redis to answer.

## Container view (docker compose)

```text
browser
   |
   v
streamlit  :8501   streamlit_app_loader.py (typed form + conversational tab)
   |  HTTP, ESTIMATOR_API_BASE_URL=http://api:8000
   v
api  :8000         uvicorn app.main:app --reload --reload-dir app
   |        |
   |        +--> LiteLLM Router / OpenAI SDK
   |             primary:   deepseek/deepseek-flash
   |             fallback:  claude-haiku-4-5-20251001
   |             aux calls: gpt-4o-mini (critic, summarizer, metadata, anchors)
   |
   v
redis  :6379       redis/redis-stack:7.4.0-v0 (RediSearch; RedisInsight on :8001)
```

Both services build from the same image (`python:3.14-slim`, dependencies installed
with `uv sync --frozen --no-install-project`) and bind-mount the repository at
`/srv/app`. The image copies `app/`, the two older Streamlit clients and
`.streamlit/`; `streamlit_app_loader.py` reaches the container through the bind
mount, so a standalone image without that mount would not have the compose client.
Changes to `pyproject.toml`, `uv.lock` or `Dockerfile` need a rebuild; Python edits
apply live.

A Postgres container (`pgvector/pgvector:pg16`, host port 5433) backs both
persistence layers. It holds four tables: `pseudonym_mappings` and
`ingestion_jobs` (ingestion, spec 011), plus `documents` and `chunks` with the
`vector` extension enabled (the RAG store, spec 012).

Outside compose the default `DATABASE_URL` is a SQLite file at `var/estimator.db`,
so the test suite and a plain `uvicorn` run need no database. `DATABASE_URL` is
the only switch between the two; the SQL is portable.

## Component map

| HTTP surface | Router | Service / collaborator | Schemas | Dependency factory |
| --- | --- | --- | --- | --- |
| `POST /api/v1/estimate` | `app/routers/estimations.py` | `app/services/llm_service.py` → OpenAI SDK direct to DeepSeek | `app/schemas/estimation_schema.py` | none (module-level `get_settings`) |
| `POST /api/v1/estimate/stream` | same router | `LLMWrapper.complete_stream` → LiteLLM Router, `EstimationCache` | same module | `get_llm_wrapper` |
| `POST /api/v1/new-estimate` | same router | `render_estimation_prompt` → `LLMWrapper.complete` → `UsageLedger` | `app/schemas/new_estimation_schema.py`, `app/schemas/usage_schema.py` | `get_llm_wrapper`, `get_usage_ledger` |
| `GET /api/v1/usage` | same router | `UsageLedger.totals` | `usage_schema.py` | `get_usage_ledger` |
| `POST /sessions`, `GET /sessions/{id}` | `app/routers/sessions.py` | `SessionStore` | router-local `CreateSessionResponse`, `SessionInfoResponse` | `get_session_store` |
| `POST /sessions/{id}/estimate` | same router | `EstimationService.estimate_conversational`, attachments, guardrails, compression, metadata | `new_estimation_schema.py` | `get_estimation_service`, `get_session_store` |
| `POST /sessions/{id}/estimate-acb` | same router | `EstimationService.estimate_with_acb` → `Boss` + `Critic` | `new_estimation_schema.py`, `acb.py`, `critic.py` | same |
| `GET /health` | `app/main.py` | `Settings` | plain dict | none |
| `POST /embeddings/ingest` | `app/embedding_pipeline/router.py` | `RagIngestService` → chunker + `OpenAIEmbedder` + `ChunkStore` (one transaction) | `app/embedding_pipeline/schemas.py` | `get_rag_ingest_service` |
| `POST /embeddings/compare` | same router | `ChunkingComparator` → eight `Chunker` strategies + `OpenAIEmbedder` (nothing persisted) | `app/embedding_pipeline/analysis/comparison.py` | `get_embedder`, `build_chunkers` |
| `POST /search` | `app/embedding_pipeline/search_router.py` | `SemanticRetriever` → embedder + `ChunkStore.search` | same module | `get_semantic_retriever` |
| `POST /api/v1/ingestion/runs`, `GET /api/v1/ingestion/jobs/{id}` | `app/routers/ingestion.py` | `app/ingestion/orchestrator.py` + repositories | `app/schemas/ingestion.py` | catalog, loader, registry, job session factory |

Fourteen paths appear in the OpenAPI document. `/docs` and `/redoc` are FastAPI's own
pages, served because `app/main.py` passes `docs_url` and `redoc_url` explicitly.

### Layering

```text
app/routers/*          HTTP only: parse, delegate, translate exceptions
        |
        v
app/services/*         pipeline logic, provider calls, caches, orchestration
        |     \
        |      +--> app/prompts/loader.py (Jinja2 templates on disk)
        |      +--> app/guardrails/*   (input layers, output filter)
        |      +--> app/sessions/*     (models, store, tiers, compression, metadata)
        |
        v
app/schemas/*          Pydantic contracts; validators are the first line of defence
```

`app/services/llm_service.py` is the exception: it bypasses the wrapper entirely and
talks to the DeepSeek API with the OpenAI SDK, with no cache, no fallback and no
ledger entry.

Dependency factories in `app/dependencies.py` are `lru_cache`d and lazy. They build
`EstimationCache`, `LLMWrapper`, `UsageLedger`, the OpenAI client, the semantic cache,
`EstimationService` and `SessionStore` on first use, which is what lets a test replace
any one of them through `app.dependency_overrides`.

## Request flows (compact)

Legacy transcription:

```text
POST /api/v1/estimate
  -> generate_estimation -> build_system_prompt (Python + context/examples.py)
  -> OpenAI SDK, max_tokens=8000 -> truncation guard -> cost_llm (4 dp)
  -> 200 EstimationResponse{estimation, model, provider, usage}
```

Streaming:

```text
POST /api/v1/estimate/stream
  -> wrapper.complete_stream
  -> cache lookup (estimation:v2:<sha256>)
       hit  -> one token frame + usage frame (all zeros)
       miss -> LiteLLM Router -> token frames -> usage frame -> cache.set
  -> done frame; any exception -> error frame (status already 200)
```

Typed:

```text
POST /api/v1/new-estimate
  -> render_estimation_prompt(version="v1")
  -> wrapper.complete (default max_tokens=4000)
  -> cache hit? -> 200 with cached tokens, cost 0.0
  -> cache miss -> Router -> normalise -> cache.set
  -> ledger.record -> 200 {text, prompt_version, cache_hit, usage}
```

Conversational:

```text
POST /sessions                    -> SessionStore.create -> 201 {session_id}
POST /sessions/{id}/estimate
  -> session lookup (404) -> attachment extraction (415/422) -> fence into transcript
  -> input guardrail (400) -> resolve tier -> render v3 prompt
  -> messages = [system] + [summary?] + anchors + window + current user
  -> Instructor structured call -> validators -> output filter
  -> append one turn -> compression -> metadata refresh -> TurnObservation
  -> 200 StructuredEstimationResponse (502 on provider failure)
GET  /sessions/{id}               -> metadata, window size, anchors, summary, last tier
```

Actor-Critic-Boss:

```text
POST /sessions/{id}/estimate-acb
  -> same prelude and guardrail -> resolve tier
  -> Boss.run(max_iterations=BOSS_MAX_ITERATIONS)
       actor(feedback)  -> prompt re-rendered with <critic_feedback>
       critic(draft)    -> CriticFeedback (accept | needs_iteration | reject)
       decision         -> accept | iterate | synthesize
  -> one assistant turn appended (only the final result)
  -> 200 ACBResponse (structured result + BossTrace)
```

Full step-by-step detail, including state and cost semantics, is in
[data-flows.md](data-flows.md).

## Where state lives, and what a restart destroys

| State | Stored in | API restart | uvicorn `--reload` (code save) | Second worker |
| --- | --- | --- | --- | --- |
| Usage ledger (`UsageLedger`) | process memory | lost | lost | forked, not shared |
| Sessions (`SessionStore`) | process memory (plain dict) | lost | lost | forked, not shared |
| Exact-match cache entries | Redis, `estimation:v2:<sha256>` | survives | survives | shared |
| Semantic cache index | Redis, index `estimations`, prefix `estimation:semantic` | survives | survives | shared |
| Settings | `lru_cache`d per process | rebuilt | rebuilt | per worker |
| Prompt templates | files under `app/prompts/` | survive | survive | shared |
| Vector store (`documents`, `chunks`) | Postgres volume | survives | survives | shared |
| Ingestion jobs, pseudonym mappings | Postgres volume (or `var/estimator.db`) | survives | survives | shared |
| Browser state (`st.session_state`) | Streamlit process | lost | lost on rerun | per session |

Two consequences worth remembering:

- Redis entries survive an API restart, but `docker compose down` removes the Redis
  container and, with it, the cache: the compose stack declares no volume for it.
- Sessions and totals are per worker. Running uvicorn with more than one worker
  breaks the conversational guarantee; the code assumes one.

## Degradation matrix

| Failure | Behaviour |
| --- | --- |
| Redis unreachable | `EstimationCache.get`/`set` log `cache_get_failed`/`cache_set_failed`; requests proceed uncached |
| `OPENAI_API_KEY` absent | `get_openai_client` returns `None`; semantic cache disabled; moderation skipped; embeddings unavailable |
| Redis without RediSearch | `get_semantic_cache` returns `None` before building the vectorizer |
| Moderation call fails | Input moderation fails open and logs `moderation_call_failed`; regex layers still run |
| Critic call fails | `CriticFeedback(verdict="accept", confidence_in_review=0)` — the draft passes through |
| Summarizer call fails | The previous rolling summary is kept |
| Anchor LLM classifier fails | Falls back to the regex detector |
| Metadata LLM extractor fails | Merged heuristic metadata is kept |
| Auxiliary model provider fails | One retry on `AUX_FALLBACK_MODEL` for calls that pass `model_override` |
| Streaming error mid-response | `error` SSE frame; the HTTP status was already 200 |

## Session-7 and Session-8 subsystems

Three subsystems were added after Session 6 and are part of the running service:

- **Embedding + vector store** (specs [010](../specs/010-session-7-embedding-pipeline.md),
  [012](../specs/012-session-8-pgvector-and-search.md)) —
  `app/embedding_pipeline/`, exposed as `POST /embeddings/ingest` and
  `POST /search`. Chunks budget components structurally, embeds them with
  `text-embedding-3-small`, and persists each budget as a `documents` row plus
  its embedded `chunks` in **one transaction**. Retrieval is k-NN by cosine
  distance in SQL, with a sequential scan and no vector index.
- **Data ingestion** (spec [011](../specs/011-session-7-data-ingestion.md)) —
  `app/ingestion/` + `app/persistence/`, exposed as
  `POST /api/v1/ingestion/runs` and `GET /api/v1/ingestion/jobs/{job_id}`.
  Catalog-validated, cleaned, validated and pseudonymized parsing with a job row
  per run. Persists job rows and pseudonym mappings; not the documents.

### Two database stacks, one URL

`app/persistence/database.py` owns both:

| Stack | Driver | Used by | Session helpers |
| --- | --- | --- | --- |
| sync | `psycopg` | ingestion jobs and mappings (BackgroundTasks, admin operations) | `get_engine`, `get_session_factory`, `SessionLocal`, `get_session` |
| async | `asyncpg` | `/embeddings/ingest` and `/search` (real request path) | `get_async_engine`, `get_async_session_factory` |

Both read `Settings.DATABASE_URL`; `_async_database_url()` swaps the driver token
for the async stack, so there is one configuration value. Both engines are built
**lazily** and cached per URL — importing the module opens nothing.

### Two declarative bases, on purpose

`app.persistence.models.Base` holds the portable ingestion tables (SQLite and
Postgres). `app.embedding_pipeline.store.models.RagBase` holds the Postgres-only
vector tables. A `Vector` column on the shared base would make
`Base.metadata.create_all()` fail on SQLite, which is how the ingestion tests
build their temporary database. `alembic/env.py` points `target_metadata` at both.

Both stacks are described in `data-flows.md` §6–8. Neither touches the
estimation, streaming, session or ACB paths: retrieval is its own endpoint and
`EstimationService` does not consult the corpus yet.
