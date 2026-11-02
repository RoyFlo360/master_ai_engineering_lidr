# 015 — Session 9: HNSW index and query-time `ef_search`

- **Status:** Implemented
- **Date:** 2026-11-02
- **Scope:** `alembic/versions/0003_rag_hnsw_index.py`,
  `app/embedding_pipeline/store/{models,repository,__init__}.py`,
  `tests/test_rag_store_models.py`, `tests/test_rag_postgres.py`
- **Supersedes / Superseded by:** Supersedes the "no vector index" baseline of
  spec [012](012-session-8-pgvector-and-search.md); superseded by —

## Problem

`POST /search` ranked the corpus with an exact sequential scan. Spec 012 made
that the baseline on purpose so a later session could measure an index against
it; with an empty index the only comparison available was theoretical. The
retrieval path already used cosine (`<=>`), which is the operator an HNSW index
needs — without the index, every query pays O(N) distance computations.

## Goals

- `chunks.embedding` has an HNSW index over cosine distance: `m = 16`,
  `ef_construction = 128`.
- Query-time `hnsw.ef_search = 40`, applied per transaction so it cannot leak
  across pooled connections.
- The planner actually chooses the index, and `EXPLAIN ANALYZE` shows it.
- Migration is reversible and still a no-op on non-Postgres (the host SQLite
  path).

## Non-goals

- Changing retrieval behaviour: no threshold, no metadata filter, no reranking,
  no query reformulation, no hybrid search. This is the index only.
- Making `ef_search`, `m` or `ef_construction` runtime settings. `m` and
  `ef_construction` are baked into the graph; `ef_search` is a module constant
  (`HNSW_EF_SEARCH = 40`) so the query path and its test read one value.
- `CREATE INDEX CONCURRENTLY`: it cannot run inside Alembic's transaction, and
  the corpus is small enough that a plain build is instant.
- Re-embedding or rewriting existing vectors. `vector_cosine_ops` fits the
  vectors already stored.

## Design

- `alembic/versions/0003_rag_hnsw_index.py` creates
  `ix_chunks_embedding_hnsw` with `USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 128)`, guarded by
  `op.get_bind().dialect.name == "postgresql"`. Downgrade drops the index by
  name.
- `store/models.py` declares the same index on `ChunkRow` so the ORM metadata
  and the database agree, and exports `HNSW_M`, `HNSW_EF_CONSTRUCTION` and
  `HNSW_EF_SEARCH`.
- `ChunkStore.search` runs
  `SET LOCAL hnsw.ef_search = 40` before the `SELECT`, inside the session's
  implicit transaction. `SET LOCAL` is transaction-scoped: the next request that
  borrows the pooled connection starts from the server default. The statement is
  skipped on non-Postgres dialects, which have neither `SET LOCAL` nor HNSW.
- `vector_cosine_ops` is mandatory. Any other operator class makes the planner
  ignore the index for `<=>` and fall back to a sequential scan **silently** —
  the failure mode is a correct answer with the old cost.

## Contract

No HTTP surface changes. `POST /search` keeps its request and response shapes;
only its execution plan changes.

```text
POST /search
{"query": "...", "k": 5}
→ 200 {query, k, search_time_ms, results[...]}
```

`SELECT ... ORDER BY embedding <=> $1 LIMIT k` is an index scan when the
planner estimates it is cheaper; at 60 rows it legitimately prefers the
sequential scan.

## Verification

- `uv run pytest -q` → `665 passed, 19 skipped` (default suite, no database).
- `TEST_DATABASE_URL=postgresql+psycopg://estimator:estimator@localhost:5433/estimator
  uv run pytest tests/test_rag_postgres.py -q` → `9 passed`, including
  `test_hnsw_vector_index_exists` and `test_search_applies_ef_search_locally`.
- Live compose stack: `alembic upgrade head` logged
  `0002_rag_pgvector -> 0003_rag_hnsw_index`; `pg_indexes` shows
  `USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='128')`.
- `scripts/query_examples.py` ingested 17 budgets / 60 chunks and all five
  `/search` queries returned ascending distances.
- With 10,000 synthetic chunks inserted and `ANALYZE` run, both the literal and
  the `PREPARE`d parameterized query chose `Index Scan using
  ix_chunks_embedding_hnsw` with `ef_search = 40` (0.8 ms execution). At 60
  chunks the planner chose the sequential scan, which is the correct choice at
  that size. The synthetic rows were deleted afterwards.

Not verified: recall against exact search on a production-scale corpus, and
performance under concurrent load. Both need a corpus larger than this
repository's sample.

## Follow-ups

- Revisit `ef_search`/`m`/`ef_construction` once there is a corpus large enough
  for a recall/latency curve; the values here are the session's assignment, not
  a measured optimum.
- `arquitectura-actual.md` still describes the sequential-scan baseline; its
  candidate list (threshold, aggregation, reranking) is unchanged by this spec.
