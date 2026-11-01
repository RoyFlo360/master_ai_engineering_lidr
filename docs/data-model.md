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

### `SearchRequest` / `SearchHit` / `SearchResponse` — `POST /search`

```text
SearchRequest{query (≤20,000), k (1..50, default 5)}
SearchHit{chunk_id, document_id, chunk_type, content, distance, metadata}
SearchResponse{query, k, search_time_ms, results[]}
```

`distance` is cosine **distance** (lower = closer). `SearchHit.chunk_id` is the
database primary key; the `BUD-…::COMP-…` id lives in `metadata`.

The query bound is 20,000 characters (raised from 2,000 in Session 9): a raw
client-meeting transcript is a legitimate query, and the probe transcripts used
for the architectural trace are 1,975–2,854 characters. At ~5k tokens the bound
stays well below the embedding model's 8,191-token input limit while still
capping an unauthenticated, billed call.

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
       embedding vector(1536) NULL, metadata JSONB, created_at}
```

Indexes: unique `source_path`, `chunk_type` (the filter vocabulary), and a GIN
index on `chunks.metadata`. **No index on `embedding`** — the sequential scan is
the baseline.

## Ingestion shapes (Session 7)

`CatalogSource`, `DataCatalog`, `Document`, `DocumentMetadata` and the
`pseudonym_mappings` / `ingestion_jobs` tables are documented in spec
[011](../specs/011-session-7-data-ingestion.md); their payloads are unchanged by
Session 8.
