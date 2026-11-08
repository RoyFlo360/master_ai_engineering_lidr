# Architecture

## Context

One FastAPI application turns a transcription or a typed brief into a software
project estimation. It is a course project: one process, no authentication, no
multi-tenancy. Persistence is Postgres for the vector store and the ingestion
job rows, and process memory for sessions and usage totals. Three Streamlit apps
are plain HTTP clients of the API.

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
`vector` extension enabled (the RAG store, spec 012). Migration `0004_rag_fts`
adds a stored `chunks.content_tsv` column and a GIN index over it; the HNSW
index is untouched.

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
| `POST /v1/retrieval/search` | `app/generation/rag/retrieval/router.py` | `pipeline.retrieve` → embedder + `ChunkStore.search`/`search_lexical` + `reciprocal_rank_fusion` + `CrossEncoderReranker` | `app/generation/rag/retrieval/schemas.py` | `get_embedder`, `get_chunk_store`, `get_retrieval_session_factory`, `get_reranker` |
| `POST /v1/estimate/from-transcript` | `app/generation/rag/estimate_router.py` | `orchestrator.estimate_from_transcript` → reformulator + `pipeline.retrieve` + context assembler + estimator + citation validation + idempotency store | `app/generation/rag/schemas.py` | router-local `get_estimate_settings`; otherwise module-level `get_settings`, lazy `get_llm_wrapper`, `get_idempotency_store` |
| `POST /v1/estimate/stages/{reformulate,retrieve,assemble,generate}` | same router | the same stage functions, called one at a time | `app/generation/rag/schemas.py` + router-local stage bodies | same |
| `POST /api/v1/ingestion/runs`, `GET /api/v1/ingestion/jobs/{id}` | `app/routers/ingestion.py` | `app/ingestion/orchestrator.py` + repositories | `app/schemas/ingestion.py` | catalog, loader, registry, job session factory |

Nineteen paths appear in the OpenAPI document. `/docs` and `/redoc` are FastAPI's own
pages, served because `app/main.py` passes `docs_url` and `redoc_url` explicitly.

### Layering

```text
app/routers/*, app/generation/rag/estimate_router.py,
app/generation/rag/retrieval/router.py
                       HTTP only: parse, delegate, translate exceptions
        |
        v
app/services/*         pipeline logic, provider calls, caches, orchestration
app/generation/rag/*   the four retrieval configurations, the estimation chain
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

Retrieval:

```text
POST /v1/retrieval/search
  -> resolve mode/rerank/rrf_k/recall_k (request field, else Settings)
  -> embed the query once
  -> vector branch: ChunkStore.search (optional distance_threshold)
  -> hybrid? lexical branch: ChunkStore.search_lexical (plainto_tsquery, ts_rank_cd)
  -> hybrid? fuse the two ranked id lists with reciprocal_rank_fusion(k=rrf_k)
  -> rerank? CrossEncoderReranker.rerank(top_n=RERANK_TOP_N)   failure -> 502
  -> 200 RetrievalResult{chunks, search_mode, rerank, candidates_evaluated,
                         search_time_ms, low_confidence}
```

Grounded estimate:

```text
POST /v1/estimate/from-transcript
  -> idempotency lookup (only with a key)          hit -> stored body, cached=true
  -> reformulate: transcript -> EstimationQuery   LLM failure -> fallback source
  -> retrieve search_query with RETRIEVAL_DISTANCE_THRESHOLD (0.65)
       no chunks -> 200 with grounded=false, citations=[], no generation call
  -> assemble: <source id=…> blocks, 3000-token budget, whole-chunk
  -> generate: one grounded LLM turn              failure -> 502
  -> validate citations against sources_of(context)
       unknown ids -> one corrective retry -> still unknown -> 502
  -> store under the idempotency key -> 200 EstimateFromTranscriptResponse
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
| Cross-encoder weights | `hf_cache` compose volume (`HF_HOME`) | survive | survive | shared |
| Ingestion jobs, pseudonym mappings | Postgres volume (or `var/estimator.db`) | survives | survives | shared |
| Estimate idempotency store (`IdempotencyStore`) | process memory, 256 entries FIFO | lost | lost | forked, not shared |
| Browser state (`st.session_state`) | Streamlit process | lost | lost on rerun | per session |

Two consequences worth remembering:

- Redis entries survive an API restart, but `docker compose down` removes the Redis
  container and, with it, the cache: the compose stack declares no volume for it.
- Sessions and totals are per worker. Running uvicorn with more than one worker
  breaks the conversational guarantee; the code assumes one. The estimate
  idempotency store has the same property: a replay may miss by landing on a
  different worker, and a restart means the estimate is generated again.

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
| Embedder or vector store unwired | `/v1/retrieval/search` returns 500 `"Embedding service is not available."`; other flows unaffected |
| Cross-encoder cannot load or score | `rerank=true` returns 502 `"Reranking service is unavailable. Please retry later."`; `rerank=false` never loads it |
| Nothing clears the estimate's distance floor | `/v1/estimate/from-transcript` returns 200 with `grounded=false`, `citations=[]`, `confidence_pct=0`; the generation model is never called |
| Reformulation LLM unavailable | The query is the truncated transcript with `source="fallback"` and confidence ≤ 0.3; the request still runs |
| Estimate generation fails | 502 `"The estimation model is unavailable. Please retry later."`; the provider error stays in the log |

## Session-7 to Session-10 subsystems

Five subsystems were added after Session 6 and are part of the running service:

- **Embedding + vector store** (specs [010](../specs/010-session-7-embedding-pipeline.md),
  [012](../specs/012-session-8-pgvector-and-search.md)) —
  `app/embedding_pipeline/`, exposed as `POST /embeddings/ingest`. Chunks budget
  components structurally, embeds them with `text-embedding-3-small`, and
  persists each budget as a `documents` row plus its embedded `chunks` in **one
  transaction**. Retrieval is k-NN by cosine distance in SQL through an HNSW
  index (spec [015](../specs/015-session-9-hnsw-index.md)). The Session-8
  `POST /search` route was retired in Session 10; query-time retrieval is the
  next bullet.
- **Query-time retrieval pipeline** (Session 10) —
  `app/generation/rag/retrieval/`, exposed as `POST /v1/retrieval/search`.
  `ChunkStore.search` supplies the vector branch and `ChunkStore.search_lexical`
  the full-text branch; hybrid mode fuses the two ranked lists with reciprocal
  rank fusion, and an optional cross-encoder reranker
  (`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, lazily loaded) reorders the
  recalled candidates. `SemanticRetriever` and `app/embedding_pipeline/retriever.py`
  survive from Session 8 but no route uses them: the `/search` router was deleted
  and `/v1/retrieval/search` calls `ChunkStore` through the pipeline instead. The
  split is deliberate: the **corpus half** (`app/embedding_pipeline/`: chunking,
  embedding, ingest, the `chunks` table) and the **query half**
  (`app/generation/rag/retrieval/`) have different lifecycles. The import edge is
  one-way — retrieval imports from `app/embedding_pipeline`, never the reverse.
- **Grounded estimation chain** (Session 10, T0B) — `app/generation/rag/`
  (the modules beside `retrieval/`), exposed as `POST /v1/estimate/from-transcript`
  and four `POST /v1/estimate/stages/*` routes. It is the only consumer of
  query-time retrieval: transcript → `EstimationQuery` → chunks → `<source>`
  context → `GroundedEstimate` → citation check. Prompts live in
  `app/prompts/rag_reformulation/v1/` and `app/prompts/rag_grounded_estimation/v1/`.
  See [data-flows.md](data-flows.md) §11 and spec
  [017](../specs/017-session-10-grounded-estimation.md).
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
| async | `asyncpg` | `/embeddings/ingest`, `/v1/retrieval/search` and the estimate chain's retrieval step (real request path) | `get_async_engine`, `get_async_session_factory` |

Both read `Settings.DATABASE_URL`; `_async_database_url()` swaps the driver token
for the async stack, so there is one configuration value. Both engines are built
**lazily** and cached per URL — importing the module opens nothing.

### Two declarative bases, on purpose

`app.persistence.models.Base` holds the portable ingestion tables (SQLite and
Postgres). `app.embedding_pipeline.store.models.RagBase` holds the Postgres-only
vector tables. A `Vector` column on the shared base would make
`Base.metadata.create_all()` fail on SQLite, which is how the ingestion tests
build their temporary database. `alembic/env.py` points `target_metadata` at both.

Both stacks are described in `data-flows.md` §6–8 and §10. Neither touches the
estimation, streaming, session or ACB paths: retrieval is its own endpoint and
`EstimationService` does not consult the corpus. The corpus is read by the
separate grounded-estimation chain described below, which calls retrieval rather
than being called from it.

### Composition: retrieval → estimation

The loop is closed. `app/generation/rag/orchestrator.py::estimate_from_transcript`
is the composition point: it calls the retrieval primitive in order to estimate,
and `POST /v1/estimate/from-transcript` mounts it. Its order is
idempotency lookup → reformulate → retrieve → assemble → generate → validate
citations (one corrective retry) → store → return.

- `query_reformulator.reformulate` turns the transcript into an
  `EstimationQuery`: a `summary` for the answer and an English `search_query`
  for the corpus, which is English while the transcripts are usually Spanish.
  Any LLM failure degrades to a truncated-transcript query with
  `source="fallback"` and confidence ≤ 0.3; only a blank transcript is fatal.
- `retrieval.pipeline.retrieve` runs the Session-10 pipeline and this caller
  passes `Settings.RETRIEVAL_DISTANCE_THRESHOLD` (0.65), so an unrelated brief
  returns no chunks instead of the five nearest. `/v1/retrieval/search` still
  applies no floor of its own.
- `context_assembler.assemble` renders the survivors as `<source>` blocks under a
  3,000-token budget, whole-chunk, rank order preserved; `sources_of` returns the
  ids that were actually emitted, and citations are checked against that set.
- `estimator.generate_estimate` makes one grounded LLM turn. An empty retrieval
  never reaches it: the chain answers `grounded=false` with no modules, no hours
  and no citations.
- `validation.check_citations` refuses a citation to a chunk that was not in the
  context, with one corrective retry before `CitationValidationError`.

The primitive/composition split survives untouched. `/v1/retrieval/search` still
returns the evidence and stops, its four configurations and their measured
precision are unchanged, and the four `POST /v1/estimate/stages/*` routes expose
each step of the chain one at a time for inspection. What the chain deliberately
does not add: metadata filters, query expansion and multi-index routing (forbidden
by the Session-10 brief), auth, and a Redis-backed idempotency store
(`app/generation/rag/idempotency.py` is process-local and dies on restart).

[`arquitectura-actual.md`](../arquitectura-actual.md) remains the Session-9
diagnosis: a real trace of a meeting transcript through the then-current
`/search`, the five failures measured from it, and the modules that would close
the loop. The modules it proposes are the ones above; the document is history,
not a description of the current gap.
