# Data model

All models are Pydantic v2. Field bounds below are the ones declared in the code, not
aspirations. Where a validator enforces a business rule, it is named.

## Usage

### `TokenUsage` — `app/schemas/estimation_schema.py`

The one usage shape in the API: `input_tokens: int`, `output_tokens: int`,
`total_tokens: int`, `cost_usd: float`. No bounds are declared. Token counts and cost
are always response data, never request fields.

### `UsageTotalsResponse` — `app/schemas/usage_schema.py`

Response of `GET /api/v1/usage`: `calls`, `cache_hits`, `input_tokens`,
`output_tokens`, `total_tokens`, `cost_usd`. A cache hit counts as a call and as a
hit, and adds no cost. The values are a snapshot of the process-local ledger, so a
restart starts them from zero.

## Estimation

### `Phase` — `app/schemas/new_estimation_schema.py`

| Field | Bound |
| --- | --- |
| `name` | 1–64 characters |
| `duration_weeks` | 1–52 |
| `cost_eur` | 0–1,000,000 |
| `summary` | 10–600 characters |

### `EstimationResult`

The validated estimation. Field order is deliberate: `phases` precedes the totals so
the model commits to per-phase numbers before summing them.

| Field | Bound / rule |
| --- | --- |
| `summary` | 10–1200 characters |
| `confidence_pct` | 0–100 |
| `phases` | 1–8 items |
| `total_duration_weeks` | 1–104 |
| `total_cost_eur` | 0–2,000,000 |

Validators:

- `phases_sum_matches_total` — `sum(phases.cost_eur)` must equal `total_cost_eur`
  exactly. Instructor re-prompts the model when it raises.
- `low_confidence_requires_out_of_scope_prefix` — when `confidence_pct < 30` the
  summary must start with `Out of scope:`. The constants are
  `LOW_CONFIDENCE_THRESHOLD = 30` and `OUT_OF_SCOPE_PREFIX = "Out of scope:"`.

`app/guardrails/output.py::enforce_scope_response` is a filter, not an exception: if a
low-confidence result reaches it without the prefix, it returns a replacement result
with one `Not estimated` phase, one week, zero cost and a prefixed summary.

### `StructuredEstimationResponse`

`result: EstimationResult`, `prompt_version: str`, `cache_hit: bool`,
`observation: TurnObservation | None` (default `None`).

Returned by the conversational endpoint and by the unwired
`EstimationService.estimate`. It has no `usage` field: the conversational path is not
cached and reports per-call billing through `observation` instead.

### `TurnObservation`

Per-turn telemetry attached only by `estimate_conversational`.

| Field | Bound |
| --- | --- |
| `turn_index` | ≥ 1 (a per-session counter, not derived from the window length) |
| `session_id` | string |
| `enriched_transcript_chars` | ≥ 0 |
| `attachments_total_chars` | ≥ 0 |
| `messages_in_window` | ≥ 0 |
| `anchors_count` | ≥ 0 |
| `summary_chars` | ≥ 0 |
| `tokens_in`, `tokens_out` | ≥ 0 (read from the raw provider completion) |
| `cost_usd` | ≥ 0 |
| `latency_ms` | ≥ 0 |
| `cache_hit_kind` | `none`, `exact` or `semantic`; default `none` |
| `last_resolved_tier` | string or `None` |

### `ACBResponse`

Extends `StructuredEstimationResponse` with `acb: BossTrace`. Returned by
`/sessions/{id}/estimate-acb`. `observation` is `None` on this path.

## Actor-Critic-Boss contracts

### `BossTrace` — `app/schemas/acb.py`

`iterations: list[ACBIteration]`, `final_decision: "accept" | "iterate" | "synthesize"`,
`iterations_run: int` (≥ 0).

`ACBIteration` records `iteration: int` (≥ 0), `decision_after`, `critic_verdict: str`,
`critic_confidence: 0–100`, and `issue_summary: list[str]` (the first five issues,
formatted `[severity] category @ field_path`).

### `CriticFeedback` — `app/schemas/critic.py`

`verdict: "accept" | "needs_iteration" | "reject"`, `issues: list[CriticIssue]`
(max 12), `confidence_in_review: 0–100`.

Validators: `needs_iteration` requires at least one issue with severity `critical` or
`major` (minor-only issues should accept); `reject` requires at least one issue.

`CriticIssue` carries `category` (one of `math_error`, `hallucination`,
`scope_mismatch`, `phase_imbalance`, `missing_assumption`, `unrealistic_estimate`,
`tier_mismatch`), `severity` (`critical | major | minor`), `field_path` (1–120
characters, dotted/bracket notation such as `phases[2].cost_eur`), `description`
(5–500 characters) and optional `suggested_fix` (≤ 300 characters).

## Conversation state

### `ProjectMetadata` — `app/sessions/models.py`

| Field | Bound |
| --- | --- |
| `project_name` | ≤ 120 characters, default `None` |
| `assumed_team_size` | 1–50, default `None` |
| `mentioned_technologies` | list of strings, default empty |
| `agreed_scope` | ≤ 2000 characters, default `None` |

`is_empty()` is true when all four are unset. `merge_with(update)` overwrites scalars
with non-`None` values and unions technologies case-insensitively, preserving the
existing order.

### `ConversationHistory`

`max_turns: int` (default 6, ≥ 1), `messages: list[Message]`,
`anchors: list[Message]`, `summary: str | None`.

- `append(user, assistant)` adds one user message and one assistant message. It does
  **not** trim; trimming belongs to `CompressionPolicy`.
- `to_messages(system_prompt=None)` returns the LLM-ready array in the order
  summary (as a synthetic user message) → anchors → recent window, with the system
  prompt prepended when supplied. The system prompt is never stored.
- `_trim()` keeps the plain window invariant for callers that only need that.

`Message` is `role: "user" | "assistant"`, `content: str`, `created_at: datetime`.

### `Session`

`session_id` (default `uuid4` string), `history: ConversationHistory`,
`metadata: ProjectMetadata`, `created_at`, `last_resolved_tier: str | None`,
`last_tier_rule: str | None`, `turns_seen: int` (default 0). Sessions live in the
`SessionStore` dict; nothing is persisted.

Router-local response models, defined inside `app/routers/sessions.py` rather than in
`app/schemas/`: `CreateSessionResponse{session_id}` and
`SessionInfoResponse{session_id, message_count, max_turns, metadata, anchors_count,
summary_chars, last_resolved_tier, last_tier_rule, summary, anchor_texts}`.

## The duplicate names

Two modules both define `EstimationRequest` and `EstimationResponse`, with different
shapes:

| Module | `EstimationRequest` | `EstimationResponse` |
| --- | --- | --- |
| `app/schemas/estimation_schema.py` | `{transcription}` (min 50) | `{estimation, model, provider, usage}` — legacy transcription reply |
| `app/schemas/new_estimation_schema.py` | `{description, project_type, detail_level, output_format}` | `{text, prompt_version, cache_hit, usage}` — typed free-text reply |

`app/routers/estimations.py` imports the typed pair as
`NewEstimationRequest` / `NewEstimationResponse` to disambiguate. The two responses are
different on purpose: the typed flow returns rendered text, while the structured flows
return a validated `EstimationResult` inside `StructuredEstimationResponse`. Do not add
a required `result` field to the free-text response; that breaks `/new-estimate`.

`TokenUsage` lives in `estimation_schema.py` and is imported by the typed module, so
there is one usage shape across both.

## Enumerations

- `ProjectType`: `mobile_app`, `web_saas`, `internal_tool`, `data_pipeline`.
- `DetailLevel`: `summary`, `medium`, `detailed`.
- `OutputFormat`: `phases_table`, `line_items`, `narrative`.
- `Tier`: `executive`, `pm`, `developer`, `default`.

## Request-side limits worth remembering

| Input | Limit |
| --- | --- |
| Legacy `transcription` | ≥ 50 characters (no maximum) |
| Typed `description` | 20–80,000 characters |
| Session `transcript` | 20–80,000 characters (`Form(min_length=20, max_length=80_000)`) |
| Estimate `transcript` | 100–50,000 characters (`MIN_TRANSCRIPT_CHARS` / `MAX_TRANSCRIPT_CHARS`) |
| Retrieval `query` | 1–20,000 characters (`MAX_QUERY_CHARS`) |
| Retrieval `top_k` / `recall_k` | 1–50 / 1–200 |
| Streaming `max_tokens` | 1–16,000 |
| Attachment text | 60,000 characters each (`MAX_ATTACHMENT_CHARS`) |
| Critic issues per review | 12 |

## Embedding and retrieval shapes

`app/embedding_pipeline/schemas.py`. The request models forbid unknown fields:
this surface is unauthenticated and every chunk costs money to embed.

### `Budget` and `BudgetComponent` — the corpus format

```text
Budget{budget_id, client_metadata{name, sector, country}, project_summary,
       main_technology, year, total_estimated_hours, components[1..100]}
BudgetComponent{component_id, name, description, tech_stack[], estimated_hours,
                complexity, dependencies[]}
```

`sector` is a closed universe (`finance | ecommerce | healthcare | industrial`)
and so is `complexity` (`low | medium | high`); a typo fails validation instead
of leaking into the metadata future filters query. `component_id` values must be
unique inside a budget, because `chunk_id` is `"{budget_id}::{component_id}"`.

### `Chunk` and `EmbeddedChunk` — the pipeline's internal unit

```text
Chunk{chunk_id, text, metadata{}, token_count}
EmbeddedChunk = Chunk + embedding[1536]
```

`text` is what the embedder sees; `metadata` travels alongside and is never
embedded. `chunk_id` is the traceable corpus id — **not** the database id.

### `IngestRequest` / `IngestResponse` — `POST /embeddings/ingest`

```text
IngestRequest{source_path (≤512, no control chars), document_type (≤50),
              content: Budget, pseudonymize: StrictBool = False}
IngestResponse{document_id, chunks_created, embedding_dimension,
               ingestion_time_ms, entities_pseudonymized = 0}
```

One request is one document. `source_path` is provenance and the uniqueness key,
not a file that is read.

`pseudonymize` is a `StrictBool`: `"yes"` is a 422, not a coercion. Turning it on
rewrites each chunk's `text` before embedding and recomputes `token_count`
(pseudonyms differ in length); `chunk_id` and `metadata` are unchanged.
`entities_pseudonymized` counts the spans replaced, so the caller can tell that
the flag did something.

### Retired: `POST /search` shapes

`SearchRequest`, `SearchHit` and `SearchResponse` remain in
`app/embedding_pipeline/schemas.py`, but no mounted route uses them: Session 10
replaced `/search` with `POST /v1/retrieval/search`, whose shapes are
`RetrievalRequest` / `RetrievedChunk` / `RetrievalResult` below. The router module
that served it was **deleted**, so `/search` answers `404`. The three models are
kept only because `SemanticRetriever.search` and the tests still construct them;
they are not a live contract.

The query bound is 20,000 characters (`MAX_QUERY_CHARS`; raised from 2,000 in
Session 9) and carries over to `RetrievalRequest.query`: a raw client-meeting
transcript is a legitimate query, and the probe transcripts used for the
architectural trace are 1,975–2,854 characters. At ~5k tokens the bound stays
well below the embedding model's 8,191-token input limit while still capping an
unauthenticated, billed call.

### `RetrievalRequest` / `RetrievedChunk` / `RetrievalResult` — `POST /v1/retrieval/search`

`app/generation/rag/retrieval/schemas.py`. The request forbids unknown fields
(`extra="forbid"`), so a typo is a 422 before any embedding call is billed.

```text
RetrievalRequest{query (1..20,000),
                 top_k (1..50, default 10),
                 recall_k (1..200, default 50; omitted -> RETRIEVAL_RECALL_TOP_K),
                 search_mode ("vector" | "hybrid" | null),
                 rerank (bool | null),
                 rrf_k (1..200 | null),
                 distance_threshold (0.0..2.0 | null)}

RetrievedChunk{chunk_id, document_id, budget_id (null), component_id (null),
               chunk_type, content, sector (default "unknown"),
               project_year (default 0), distance (float | null),
               rank (float | null),
               source ("vector" | "lexical" | "fused", default "vector")}

RetrievalResult{chunks, search_mode, rerank, candidates_evaluated (>= 0),
                search_time_ms (>= 0), low_confidence (default false)}
```

`null` on `search_mode`, `rerank` and `rrf_k` means "not specified": the router
substitutes `RETRIEVAL_SEARCH_MODE`, `RERANKER_ENABLED` and `RRF_K`. `recall_k`
has a schema default of 50, so the route checks `model_fields_set` to tell an
omitted field (use `RETRIEVAL_RECALL_TOP_K`) from an explicit 50. The request's
`rrf_k` bound (1..200) is narrower than `Settings.RRF_K` (1..1,000).

- `distance` is cosine distance from the vector branch (lower = closer). It is
  `null` for a lexical-only hit — deliberately not a sentinel. A chunk found by
  both branches keeps its vector distance.
- `rank` is `ts_rank_cd` from the lexical branch; `null` for a vector-only hit.
- `source` is provenance, not order: `vector`, `lexical`, or `fused` when both
  branches returned the chunk. Reranking reorders results and never relabels
  them.
- `candidates_evaluated` counts vector rows plus lexical rows before
  deduplication, so a five-chunk reply can report a hundred candidates.
- `low_confidence` is `true` when `chunks` is empty. It is a count of survivors,
  not a relevance threshold; `distance_threshold` filters the vector branch only.
- `budget_id` and `component_id` come from the chunk's JSONB metadata; missing
  keys degrade to `null`, `"unknown"` or `0` instead of failing the retrieval.
- Reranking keeps at most `RERANK_TOP_N` (default 5) survivors, so
  `rerank=true` can return fewer than `top_k` chunks.

## Grounded-estimation shapes

`app/generation/rag/schemas.py`. The retrieval half of the chain uses
`RetrievedChunk` (above); this module holds the generation half, and the two meet
at `RetrievedChunk`.

```text
EstimationQuery{summary (1..2,000),
                search_query (1..2,000),
                sector (≤200 | null),
                technologies (≤20),
                project_year (2000..2100 | null),
                confidence (0.0..1.0, default 0.0),
                source ("llm" | "fallback", default "llm")}

Citation{chunk_id, budget_id (str | null), claim (1..500)}
EstimatedTask{name (1..200), estimated_hours (0..10,000)}
EstimatedModule{name (1..200), tasks (1..30), estimated_hours (0..100,000)}
GroundedEstimate{summary (1..2,000), modules (≤20), total_hours (0..1,000,000),
                 citations (unbounded), confidence_pct (0..100), grounded}

EstimateFromTranscriptRequest{transcript (100..50,000),
                              idempotency_key (≤128 | null),
                              top_k (1..50, default 5),
                              rerank (bool | null),
                              search_mode ("vector" | "hybrid" | null)}
EstimateFromTranscriptResponse{estimate: GroundedEstimate,
                               query: EstimationQuery,
                               chunks (list), retrieved (≥0), cached,
                               validation_retries (0..1), pipeline_ms (≥0)}
StageEnvelope{stage ("reformulate" | "retrieve" | "assemble" | "generate"),
              latency_ms (≥0)}
```

- `summary` and `search_query` are separate on purpose: the summary is for the
  answer, the search string is what is embedded, and the corpus is English while
  the transcripts are usually Spanish. `sector` is a retrieval filter, so the
  reformulator is told to return `null` rather than guess.
- `source` says where the query came from. `"fallback"` means the LLM failed and
  the search string is a truncated transcript, with `confidence` capped at 0.3.
- `grounded` is the honesty flag: `False` means no chunk reached the prompt, and
  the estimate carries no modules, no hours and no citations. `confidence_pct` is
  `0` in that case.
- No Pydantic validator enforces `total_hours == sum(modules.estimated_hours)` or
  the per-module sum. The generation prompt states both as rules; the schema only
  bounds the values.

`EstimateFromTranscriptResponse.chunks` is declared as a bare `list` to avoid a
circular import between `app/generation/rag/schemas.py` and
`app/generation/rag/retrieval/schemas.py`. It serialises `RetrievedChunk`
objects and appears that way in the OpenAPI document.

Bounds are module constants, shared by the schemas, the router and the tests:
`MIN_TRANSCRIPT_CHARS = 100`, `MAX_TRANSCRIPT_CHARS = 50_000`,
`MAX_SEARCH_QUERY_CHARS = 2_000`, `MAX_SUMMARY_CHARS = 2_000`, `MAX_MODULES = 20`,
`MAX_TASKS_PER_MODULE = 30`, `MAX_NAME_CHARS = 200`,
`MAX_CITATION_CLAIM_CHARS = 500`, `MAX_IDEMPOTENCY_KEY_CHARS = 128`.

The four stage request models are router-local, the same convention as the session
router's response models: `StageReformulateRequest{transcript}`,
`StageRetrieveRequest{query | estimation_query, top_k (1..50), search_mode,
rerank, rrf_k (1..1,000), distance_threshold (0..2)}` with a validator requiring
exactly one of `query`/`estimation_query`, `StageAssembleRequest{chunks (≥1),
max_tokens (≥1, default 3,000)}` and `StageGenerateRequest{query, context
(≤200,000), source_ids (list of int), model_override}`.

## Chunking-comparison shapes

`app/embedding_pipeline/analysis/comparison.py`, used by
`POST /embeddings/compare` and the CLI.

```text
CompareRequest{budgets[1..100], queries[], strategies[], top_k (1..10, default 3)}
ChunkingStats{strategy, n_chunks, token_distribution{min, p50, p95, max},
              n_orphan_chunks, n_obese_chunks, ingestion_cost_usd, ingestion_seconds}
TopChunk{chunk_id, cosine, text_preview}
QueryResult{strategy, query, top_k[TopChunk]}
CompareResponse{stats_per_strategy{}, queries_per_strategy{}}
```

`top_k` is capped at 10 in the request and defaults to 3. `strategies` empty
means "every registered strategy". Orphans are chunks under 20 tokens; obese are
over 800.

## Vector-store rows

`app/embedding_pipeline/store/models.py`, on their own declarative base
(`RagBase`) because they are Postgres-only.

```text
documents{id, source_path (UNIQUE), document_type, ingested_at, metadata JSONB}
chunks{id, document_id → documents.id ON DELETE CASCADE, chunk_type, content,
       embedding vector(1536) NULL, metadata JSONB, created_at,
       content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content))
       STORED}
```

Indexes: unique `source_path`, `chunk_type` (the filter vocabulary), a GIN
index on `chunks.metadata`, HNSW on `chunks.embedding`
(`vector_cosine_ops`, `m = 16`, `ef_construction = 128`), and — since migration
`0004_rag_fts` — GIN on `chunks.content_tsv` (`ix_chunks_content_tsv`).
`ef_search` is query-time (40, set with `SET LOCAL` in `ChunkStore.search`), not
part of the index.

`content_tsv` is Postgres-only and deliberately **not mapped** on the ORM model:
mapping it would make `create_all` fail on SQLite, which the hermetic suite uses.
`ChunkStore.search_lexical` references it by column name. The text-search
configuration is the constant `FTS_REGCONFIG = "english"` in
`app/embedding_pipeline/store/repository.py`, mirrored by a literal in the
migration; it is not a setting, because an env value that disagreed with the
stored generated column would bypass the GIN index silently.

## Ingestion shapes (Session 7)

`CatalogSource`, `DataCatalog`, `Document`, `DocumentMetadata` and the
`pseudonym_mappings` / `ingestion_jobs` tables are documented in spec
[011](../specs/011-session-7-data-ingestion.md); their payloads are unchanged by
Session 8.
