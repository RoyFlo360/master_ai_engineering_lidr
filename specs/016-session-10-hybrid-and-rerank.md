# 016 — Session 10: hybrid search and cross-encoder reranking

- **Status:** Implemented
- **Date:** 2026-11-08
- **Scope:** `app/generation/rag/retrieval/` (new: `schemas.py`, `fusion.py`,
  `pipeline.py`, `errors.py`, `reranker.py`, `verify_reranker.py`, `router.py`,
  `README.md`), `app/embedding_pipeline/store/repository.py`,
  `alembic/versions/0004_rag_fts.py`, `app/config.py`, `app/main.py`,
  `app/dependencies.py`, `evals/golden_retrieval.json`,
  `scripts/eval_retrieval_s10.py`, `session10_report.md`, `docs/**`, `AGENTS.md`,
  and the migrated callers of the retired route (`scripts/query_examples.py`,
  `examples/trace_s09.py`, `tests/test_search_endpoint.py`)
- **Supersedes / Superseded by:** Supersedes the retrieval surface of spec
  [014](014-session-9-prework-trace.md) — `POST /search` is retired; the
  20,000-character query bound it introduced survives on the new route — and the
  single-ranking baseline of spec [012](012-session-8-pgvector-and-search.md) and
  spec [015](015-session-9-hnsw-index.md). Superseded by —.

## Problem

Retrieval had exactly one ranking and no way to improve it. `POST /search`
embedded the query, ran one cosine k-NN over `chunks`, and returned the top `k`:

- **One lexical blind spot.** Dense retrieval cannot see an identifier it never
  learned. A query naming `BUD-2024-013` or a component id returns "the nearest
  budget-shaped prose", not the row that contains the token. There was no
  full-text path to combine with.
- **One, untuneable ranking.** Relevance was whatever cosine similarity said.
  There was no second stage to reorder candidates for a specific query, and no
  way to compare "cheap ranking" against "expensive ranking" on the same corpus
  — which is exactly the measurement the session was asked for.
- **No surface for a configuration.** Even if both existed, `/search` had no
  field to turn them on. A reviewer could not point at a request that means
  "hybrid, reranked"; every configuration would have been a code edit.

The measured consequences are in `session10_report.md`: on the 5-query golden
set, precision@5 is 0.360 for vector and for hybrid, 0.320 for either reranked
configuration, at 259 ms / 253 ms / 9,408 ms / 9,459 ms mean latency.

## Goals

- A lexical branch over `chunks.content` that finds tokens the vector branch
  misses, indexed so it is not a table scan.
- Reciprocal Rank Fusion of the two branches, with a 1-based, tested formula.
- A cross-encoder reranker behind a switch, loaded lazily, failing closed.
- The four configurations — vector/hybrid × rerank on/off — reachable from one
  HTTP request, one function and one evaluation CLI, so the numbers a client
  sees and the numbers in the report cannot drift.
- One retrieval contract. `/search` is retired once the replacement is proven,
  not kept beside it.

## Non-goals

- **Metadata filters.** `search_filtered(sectors=…, project_year_min=…)` exists
  in the reference; the brief forbids metadata filtering, so the only filter here
  is `distance_threshold`, a relevance floor, and it applies to the vector
  branch only.
- **Query expansion.** Forbidden by the brief. The query is embedded and
  full-text-searched exactly as sent.
- **Multi-index routing.** Forbidden by the brief. There is one `chunks` table
  and one GIN index.
- **Grounded generation.** The Session-9 generation chain (reformulator, context
  assembler, grounded estimator, citation validation, idempotency) was approved
  as T0B and **did not ship**. Retrieval returns chunks; nothing turns them into
  an estimate.
- **Auth on the retrieval route.** No route in this repository declares an auth
  scheme; adding one here only would make the service internally inconsistent.
  The query-length cap remains the spend guard.
- **Tuning the golden set to fit a hypothesis.** The four configurations are
  reported as measured, including the parts that do not flatter the feature.

## Design

### The decision: a versioned route, and `/search` retired (Proposal A)

Two options were on the table.

- **Proposal A (chosen):** a new `POST /v1/retrieval/search` with explicit
  `search_mode`, `rerank`, `recall_k`, `rrf_k` and `distance_threshold` fields,
  then delete `/search` once the new route passed the four-configuration run.
- **Proposal B (rejected):** grow `SearchRequest` with three defaulted fields so
  omitting them reproduced today's behaviour.

B is genuinely less code and would have produced the same table. It was rejected
because it makes one endpoint carry two result semantics, and because keeping
both routes would reproduce the reference implementation's known defect: two
retrieval paths where the older one silently ignores the new switches. A versioned
route plus deletion leaves one contract, and matches the surface the course's
reference exposes, so a reviewer comparing artefacts finds parity without
translation.

### The split: two packages, one one-way edge

`app/embedding_pipeline/` is the **corpus half** — chunking, embedding, ingest,
the `chunks` table, `/embeddings/*`. `app/generation/rag/retrieval/` is the
**query-time half** — lexical search, fusion, reranking, the four
configurations, `/v1/retrieval/search`. `app/generation/rag/retrieval/README.md`
is the single place that states the boundary.

The edge is one-way: the retrieval package imports `ChunkStore`,
`OpenAIEmbedder` and the schemas the corpus half owns; nothing under
`app/embedding_pipeline/` imports `app.generation`. `tests/test_retrieval_maintainability.py`
asserts that direction so a "small" convenience import cannot close the cycle.

The brief's own command path (`app/generation/rag/retrieval/`) is therefore
satisfied without moving 133 import lines across 19 files. One deviation is
recorded in that README and in the report: our compose service is named `api`,
so the brief's `docker compose exec ai-service …` is `docker compose exec api …`
here.

### The `english` FTS configuration, and the corpus-language evidence

The brief says the dataset entries are in Spanish. In this repository, the corpus
that is actually chunked and embedded is **English**:

- `data/budgets_sample.json` — the 17 budgets mapped into `Budget` and rendered
  by `chunking/structural.py` — reads *"Mobile banking API with OAuth 2.0
  authentication and PSD2 compliance"*, and the chunk template emits
  `[Project: …] [Client sector: … | Year: …]`, also English.
- `data/seed/budgets/*.json` **is** Spanish, but it feeds the Session-7
  ingestion pipeline (`client_name` / `phases`) and is never embedded.
- The course's own reference solution recorded the same contradiction in its
  `0003_session10_fts.py`, and its sample corpus is English with the same ids
  and the same `project_summary` values.

So the column is `to_tsvector('english', content)` and the query is
`plainto_tsquery('english', $1)`. Following the brief literally with `'spanish'`
stems English words with Spanish rules and applies a stop-word list that never
matches: the index still builds, the recall quietly drops, and nothing fails.

The value is one constant in two places — the DDL literal in
`alembic/versions/0004_rag_fts.py` and `FTS_REGCONFIG` in
`app/embedding_pipeline/store/repository.py` — and
`tests/test_fts_regconfig.py` parses both plus `Settings` and fails on drift. It
is deliberately **not** an environment setting: an `.env` value that disagrees
with the generated column bypasses the GIN index without raising anything.
Switching language later is a `DROP COLUMN` / `ADD COLUMN` rewrite plus a GIN
rebuild, not an in-place alteration.

### The four configurations

`app/generation/rag/retrieval/pipeline.py::retrieve` is the only place that
knows how the stages compose:

```text
vector / no rerank : vector top_k
hybrid / no rerank : vector recall_k + lexical recall_k -> RRF -> top_k
vector / rerank    : vector recall_k -> reranker -> top_k
hybrid / rerank    : vector recall_k + lexical recall_k -> RRF -> reranker -> top_k
```

Defaults come from `Settings` (`RETRIEVAL_SEARCH_MODE="vector"`,
`RERANKER_ENABLED=False`, `RRF_K=60`, `RETRIEVAL_RECALL_TOP_K=50`,
`RERANK_TOP_N=5`, `RERANKER_MODEL="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"`),
so a deployment switches configuration without editing code, and a request
overrides it per call.

### Deviations from the reference implementation

These are the places where we deliberately do not copy the reference, each for a
reason that is visible in behaviour or in a test.

1. **`source` provenance instead of a sentinel distance.** The reference encodes
   "this hit came from the lexical branch" by writing a sentinel into
   `distance`, which makes one field carry two meanings and leaves the caller no
   way to tell them apart. `RetrievedChunk.source` is `"vector"`, `"lexical"` or
   `"fused"`, and `distance` keeps its one meaning.
2. **`distance=None` for a lexical-only hit.** Not `0.0`, not `-1`, not the RRF
   score. A chunk that no vector query ranked has no cosine distance; inventing
   one is how a downstream threshold or a sort silently treats it as a perfect
   match. `rank` carries the lexical `ts_rank_cd` beside it.
3. **`candidates_evaluated` counts both branches, before dedup.** The reference
   reports the size of the fused list, which is the answer, not the work. Here it
   is `len(vector_rows) + len(lexical_rows)`, so a hybrid run over 50 + 50 that
   fuses to 5 reports 100 — and a reader can see that the lexical branch
   contributed almost nothing (the report's actual finding: 51 versus 50).
4. **RRF is 1-based.** The score is `sum(1 / (k + rank))` with `rank` starting at
   1, and the docstring says so. The reference used 0-based positions, which
   shifts every score and changes the relative weight of a document that appears
   in both branches. `tests/test_rrf_fusion.py` pins the arithmetic.
5. **Fail-closed 502 instead of a silent fallback.** When the caller asks for
   `rerank=true` and the cross-encoder cannot load or score, the route returns
   `502 {"detail": "Reranking service is unavailable. Please retry later."}` and
   logs `reranker_unavailable` with the error type. It never falls back to recall
   order: a 200 would report a configuration that never ran, and the evaluation
   harness would record it as config C or D. The reference also aborts on failure
   rather than degrading — so the *policy* is shared, and what is ours is the
   public message, the log event, and the guarantee (tested) that the un-reranked
   paths keep returning 200 while the same broken reranker is wired.

## Contract

### `POST /v1/retrieval/search`

No auth. `RetrievalRequest` forbids unknown fields (`extra="forbid"`).

```text
POST /v1/retrieval/search
{
  "query": "…",                     # 1..20,000 chars, required
  "top_k": 10,                      # 1..50,  default 10
  "recall_k": 50,                   # 1..200, default 50
  "search_mode": "vector" | "hybrid" | null,
  "rerank": true | false | null,
  "rrf_k": 60 | null,               # 1..200
  "distance_threshold": 0.4 | null  # 0..2, vector branch only
}
→ 200
{
  "chunks": [
    {"chunk_id": 7, "document_id": 3, "budget_id": "BUD-2024-001",
     "component_id": "AUTH-001", "chunk_type": "budget_component",
     "content": "…", "sector": "finance", "project_year": 2024,
     "distance": 0.231, "rank": null, "source": "vector"}
  ],
  "search_mode": "vector",
  "rerank": false,
  "candidates_evaluated": 10,
  "search_time_ms": 87,
  "low_confidence": false
}
```

- `null` on `search_mode`, `rerank`, `rrf_k` means "use the deployment default";
  `null` on `distance_threshold` means "no floor". `recall_k` has a schema
  default of its own, so the route distinguishes "omitted" (use
  `RETRIEVAL_RECALL_TOP_K`) from "50 sent explicitly" via
  `model_fields_set`.
- `low_confidence` is `len(chunks) == 0` — an empty corpus is a 200, not a 404.

Failure matrix:

| Status | Body | Raised when |
| --- | --- | --- |
| 422 | FastAPI validation detail | any bound fails, an unknown field, an unknown `search_mode`, an empty or over-long query — before any I/O |
| 502 | `{"detail": "Reranking service is unavailable. Please retry later."}` | `rerank=true` and the cross-encoder failed to load or score |
| 500 | `{"detail": "Embedding service is not available."}` | no embedder or no store wired |
| 500 | `{"detail": "Failed to run retrieval."}` | any other failure; the exception text goes to the log, never to the client |

### `ChunkStore`

- `search(session, *, query_vector, k, distance_threshold=None)` — the
  pre-Session-10 statement plus an optional `WHERE distance <= threshold` on the
  expression (not the SELECT alias). `None` reproduces the old query exactly, so
  existing callers are unaffected.
- `search_lexical(session, *, query_text, top_k=50)` — `plainto_tsquery` against
  `content_tsv`, ordered by `ts_rank_cd` descending. Row labels mirror `search`
  (`chunk_id`, `document_id`, `chunk_type`, `content`, `metadata`) plus `rank`
  and **no** `distance`. `plainto_tsquery` rather than `to_tsquery` because a raw
  transcript contains `:`, `&` and `|`, which `to_tsquery` rejects as syntax.
- No commit, no session ownership: the store still never opens or commits.

### Migration `0004_rag_fts`

```sql
ALTER TABLE chunks ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX ix_chunks_content_tsv ON chunks USING gin (content_tsv);
```

Postgres only, guarded by `op.get_bind().dialect.name == "postgresql"` exactly
like `0002_rag_pgvector` and `0003_rag_hnsw_index`, so the host
`alembic upgrade head` against SQLite still creates the ingestion tables.
`downgrade()` drops the index and the column. `ChunkRow` deliberately does not
map `content_tsv`: the ORM model would break `Base.metadata.create_all()` in the
SQLite fixtures, so the lexical query references the column by name. The HNSW
index from spec 015 is neither dropped nor recreated.

### Retired surface

`app/embedding_pipeline/search_router.py` is deleted, and `app/main.py` loses its
import and `include_router` line. `SemanticRetriever`,
`ChunkStore.search`, `SearchRequest`/`SearchResponse`/`SearchHit` and
`get_semantic_retriever` stay: the models describe the retriever's return value
and the real-database tests still drive `SemanticRetriever.search`. Only the
route was retired.

## Verification

Implemented 2026-11-08. Every number below is copied from a run, not estimated.

- **Default suite** — `uv run pytest -q` → **777 passed, 24 skipped, 3 warnings**.
  Baseline before Session 10 was 665 passed / 19 skipped.
- **With the real database** — `TEST_DATABASE_URL=postgresql+psycopg://estimator:estimator@localhost:5433/estimator
  uv run pytest -q -m postgres` → **24 passed**.
- **Migration round-trip on real Postgres** —
  `tests/test_retrieval_postgres.py::test_migration_round_trip_drops_and_restores_content_tsv`
  runs `upgrade head` → `downgrade -1` → `upgrade head`: `content_tsv` and
  `ix_chunks_content_tsv` appear, disappear, and return, while
  `ix_chunks_embedding_hnsw` survives both directions.
- **Reranker gate** — `docker compose exec api python -m
  app.generation.rag.retrieval.verify_reranker` → exit **0**, the relevant
  document scored 10.78 against an irrelevant −8.55.
- **The four configurations** — `session10_report.md` holds the golden set, the
  A/B/C/D precision and latency table, the per-query breakdown, the side-by-side
  scoring that shows the reranker works, and the conclusions. The harness is
  `scripts/eval_retrieval_s10.py` against `evals/golden_retrieval.json`.
- **Unit and hermetic coverage** —
  `tests/test_rrf_fusion.py` (1-based arithmetic, dedup within a ranking, empty
  branch, tie-break, `k <= 0`), `tests/test_reranker.py` (ordering, `top_n`,
  `text_of`, and that importing the module leaves `torch` and
  `sentence_transformers` out of `sys.modules`), `tests/test_hybrid_retrieve.py`
  (all four configurations, `candidates_evaluated` counting both branches,
  lexical-only hits labelled correctly), `tests/test_fts_regconfig.py` (the DDL
  literal, the query constant and `Settings` cannot drift),
  `tests/test_retrieval_route.py` (200 for every configuration, defaults from
  `Settings`, 422 bounds, the 502 body, and 200 on the un-reranked paths while a
  broken reranker is wired), `tests/test_retrieval_security.py` (caps, closed
  sets, no query text or key material in a failure body, model name only from
  `Settings`), `tests/test_retrieval_maintainability.py` (one-way import edge,
  no duplicated regconfig literal, no torch at import).
- **Real-database lexical behaviour** — `tests/test_retrieval_postgres.py`
  proves `search_lexical` finds a rare identifier the vector branch misses,
  returns nothing for a stop-word-only query, and that the two branches can
  disagree (the premise of hybrid search).
- **Retired surface** — `tests/test_search_endpoint.py` asserts `POST /search`
  is a 404 and absent from `/openapi.json`, and that
  `/v1/retrieval/search` publishes `RetrievalRequest` → `RetrievalResult` with
  the bounds above.

Not verified:

- **Mutation score.** Measured, and the gate passes on the reachable set: 557
  mutants, 479 killed (86% overall, 98.6% of the 486 a selected test reaches), 7
  survivors triaged as equivalent or as mutmut mis-reporting a default-argument
  mutation, 71 unmapped (pre-Session-10 `ChunkStore` CRUD). Reproduced with
  `docker compose exec api sh -c "cd /srv/app && uv run mutmut run"`; the numbers are
  in `session10_report.md` §7. The harness cannot run on native Windows.
- **A lift from hybrid search or reranking.** On this 60-chunk corpus neither
  improves precision@5; the report says so plainly and scopes the conclusion to
  the corpus size.
- **Spanish queries against the `english` configuration.** All five golden
  queries are English, like the corpus.

## Follow-ups

- Re-measure the four configurations on a corpus an order of magnitude larger.
  At 60 chunks dense retrieval already returns roughly four of every five
  distinct budgets correctly, so there is nothing for a second stage to fix.
- Design a judged-relevance or NDCG metric. Precision@5 over distinct budgets
  cannot express the two improvements the reranker demonstrably produced
  (removing a false positive, concentrating on the best analogue).
- Keep re-running the mutation gate in CI: the container run is reproducible and the
  four scoped modules are what it protects. Raise the threshold only when the
  unmapped 71 (`ChunkStore` CRUD) are either covered or moved out of `source_paths`.
- Spec 014 remains Implemented; only its retrieval surface is superseded by this
  one.
