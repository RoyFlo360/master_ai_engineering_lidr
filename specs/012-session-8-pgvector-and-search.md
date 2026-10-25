# 012 — Session 8: pgvector persistence and semantic search

- **Status:** Implemented
- **Date:** 2026-10-24
- **Scope:** `app/embedding_pipeline/` (`schemas`, `router`, `chunker`,
  `embedder`, new `store/`, `ingest_service`, `retriever`, `search_router`),
  `app/persistence/database.py`, `app/dependencies.py`, `app/main.py`,
  `alembic/env.py`, `alembic/versions/0002_rag_pgvector.py`,
  `scripts/query_examples.py`, `data/budgets_sample.json`,
  `tests/test_rag_*.py`, `tests/test_embeddings_*.py`,
  `tests/test_search_endpoint.py`, `output_examples.txt`, `README.md`,
  `AGENTS.md`, `docs/`, `rules/`
- **Supersedes / Superseded by:** supersedes the **contract** of spec 010
  (the pipeline and chunker survive; the response shape does not). Spec 010
  stays `Implemented` as the record of what Session 7 shipped.

## Problem

Session 7 ends with the embedding pipeline returning vectors over HTTP and
discarding them. The vectors therefore cannot be searched: answering "which
past project is most like this one" would require re-embedding the corpus on
every request, at full cost and latency.

The Session-8 brief (`instructions_session8.md`) asks for the next step:
persist each ingested budget as a `document` with its `chunks` and their
embeddings in Postgres + pgvector, in a single transaction, and expose a
semantic search endpoint that ranks chunks by cosine distance in SQL.

Two constraints from our own repo shape this work:

1. `DATABASE_URL` defaults to SQLite (`var/estimator.db`) so the 544-test suite
   and a laptop run need no database. `CREATE EXTENSION vector`, `JSONB` and
   `Vector(1536)` are Postgres-only.
2. `tests/test_ingestion_persistence.py` builds a temp database with
   `Base.metadata.create_all(engine)` on SQLite. A vector column on the shared
   `Base` would make that raise `CompileError` and break ten passing tests for
   a reason unrelated to them.

## Goals

- An Alembic migration `0002_rag_pgvector` that enables the `vector` extension
  and creates `documents` and `chunks` with their relational and JSONB indexes,
  on Postgres, while leaving a host SQLite `upgrade head` working.
- `POST /embeddings/ingest` persists one document plus N embedded chunks in a
  single transaction and returns identifiers and metrics, not vectors.
- A duplicate `source_path` is a `409` and cannot create a second document.
- An embedder failure leaves **no** `documents` row.
- `POST /search` returns the k chunks nearest a free-text query by cosine
  distance, computed in SQL.
- The default `pytest` run stays hermetic: no Postgres, no OpenAI, no network.

## Non-goals

- **Vector indexes.** HNSW/IVFFlat are the live session; the sequential scan is
  the baseline it measures against. `tests/test_rag_store_models.py` asserts no
  index covers `embedding`.
- **Metadata filtering in SQL** (`WHERE chunk_type = …`, `metadata->>'sector'`),
  **hybrid search** (full-text + vector) and **Postgres parameter tuning**. All
  four are named as live-session work by the brief.
- Retrieval inside `EstimationService.estimate()`. `/search` is its own
  endpoint; wiring retrieval into the estimation prompt is a later session.
- Reranking, recall@k/NDCG, retrieval evaluation harnesses.
- Document update/delete endpoints, re-embedding, or pgvector on SQLite.
- Changing the estimation, streaming, session, ACB, guardrail, attachment or
  ingestion behaviour.

## Design

### Two declarative bases, not one

The RAG tables live on their own base:

```python
# app/embedding_pipeline/store/models.py
class RagBase(DeclarativeBase):
    """Vector-store schema: Postgres-only."""
```

`alembic/env.py` sets `target_metadata = [Base.metadata, RagBase.metadata]`
(Alembic accepts a sequence), so autogenerate and `alembic check` see both.

Rejected: putting `Vector(1536)` on the shared `Base`. It breaks
`Base.metadata.create_all()` on SQLite — the mechanism that keeps the existing
ingestion tests database-free. A `Vector → NullType` shim was also rejected: it
hides a Postgres-only reality behind a type that lies, and `alembic check`
would then disagree with the real DDL. A test asserts `Base.metadata` contains
no vector column, so this cannot regress silently.

### Migration is dialect-guarded

`0002_rag_pgvector.py` returns early unless the connection is Postgres:

```python
def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
```

`alembic upgrade head` runs on the host against SQLite **and** in compose
against Postgres. Without the guard the host path fails on `CREATE EXTENSION`.
`downgrade()` drops the indexes and tables but leaves the extension installed:
dropping it is a cluster-level operation, and `IF NOT EXISTS` makes re-upgrade
free.

### Two database stacks, one URL

Sync `psycopg` keeps serving the ingestion paths (BackgroundTasks, admin
operations). The RAG store uses async `asyncpg`, as the brief requires and
because `/embeddings/ingest` and `/search` are on the real request path. One
`DATABASE_URL` configures both: `_async_database_url()` swaps the driver token
(`+psycopg` → `+asyncpg`, bare `postgresql://` → `postgresql+asyncpg://`).

The async engine and session factory are **lazy** and cached per URL, mirroring
the existing sync helpers. `import app.persistence.database` must still create
no engine and no SQLite file. The reference's eager module-level `SessionLocal`
is not copied.

### The store never owns a transaction

`ChunkStore` takes an `AsyncSession` from its caller and never opens, commits or
rolls back. The ingest service and the retriever own the session, so the
duplicate check, the document insert and the chunk inserts share one
transaction. A failure anywhere discards all of it — that is what makes "no
orphan document" true rather than aspirational.

`search` composes the distance expression once and orders by it:

```python
distance = ChunkRow.embedding.cosine_distance(query_vector)
stmt = select(..., distance.label("distance")).order_by(distance).limit(k)
```

**Cosine, not L2 or inner product.** OpenAI embeddings are normalized, so the
ranking would be equivalent; cosine is chosen because it matches the RAG
literature and, more concretely, the `vector_cosine_ops` operator class of the
HNSW index the live session adds. An operator/index mismatch makes Postgres
ignore the index silently.

### Duplicate handling is belt and braces

`documents.source_path` carries a **unique** index (a deliberate one-line
strengthening of the brief's plain index). The service checks first, which
produces the friendly 409; if a concurrent writer wins the race, the
`IntegrityError` is caught, the existing id is re-selected, and the same 409 is
returned. The check gives the UX, the index makes the invariant real.

### The dimension is a constant with a guard

`EMBEDDING_DIMENSIONS = 1536` is defined once in the store module and reused by
the migration and the embedder. `Settings.EMBEDDING_MODEL` must be the model
with that width (`text-embedding-3-small`); when it is not, the RAG dependency
factories return the "vector store unavailable" state instead of writing
vectors that would make every later distance meaningless.

### Files

| Path | Role |
| --- | --- |
| `app/embedding_pipeline/store/models.py` | `RagBase`, `DocumentRow`, `ChunkRow`, `EMBEDDING_DIMENSIONS` |
| `app/embedding_pipeline/store/repository.py` | `ChunkStore`: `find_document_id`, `persist_document_with_chunks`, `search` |
| `app/embedding_pipeline/ingest_service.py` | `RagIngestService.ingest`, `DuplicateDocumentError` |
| `app/embedding_pipeline/retriever.py` | `SemanticRetriever.search` |
| `app/embedding_pipeline/router.py` | `POST /embeddings/ingest` (rewritten) |
| `app/embedding_pipeline/search_router.py` | `POST /search` |
| `app/persistence/database.py` | `is_postgres_url`, `_async_database_url`, async engine/factory |
| `alembic/versions/0002_rag_pgvector.py` | extension, `documents`, `chunks`, indexes |

## Contract

### `POST /embeddings/ingest` — one document per request

```
{"source_path": "data/budgets_sample.json::BUD-2024-001",
 "document_type": "historical_budget",
 "content": <Budget>}
→ 200 {"document_id": 1, "chunks_created": 4,
        "embedding_dimension": 1536, "ingestion_time_ms": 412}
```

- **422** — Pydantic validation of `content` as a `Budget` (bad enum,
  out-of-range year/hours, no components, unknown field), `source_path` empty,
  over 512 characters, or containing control characters, `document_type` over
  50 characters.
- **409** — `{"detail": "Document already ingested", "document_id": 42}`.
  Top-level body, not nested under `detail`; a documented exception to
  `rules/api.md`, alongside the ingestion router's flat error bodies.
- **500** — `{"detail": "Embedding service is not available."}` when no
  `OPENAI_API_KEY` is configured, when `DATABASE_URL` is not Postgres, or when
  `EMBEDDING_MODEL` is not the 1536-dim model; `{"detail": "Failed to generate
  embeddings."}` for any embedding or database failure. Reasons go to the log,
  never the response.

Vectors no longer travel over HTTP. The Session-7 batch body
(`{"budgets": [...]}`) now returns **422**; a test pins that, so a stale client
fails loudly instead of silently posting into a contract that no longer exists.

### `POST /search`

```
{"query": "REST API with OAuth authentication", "k": 5}
→ 200 {"query": "...", "k": 5, "search_time_ms": 87,
        "results": [{"chunk_id": 156, "document_id": 12,
                     "chunk_type": "budget_component", "content": "...",
                     "distance": 0.231, "metadata": {...}}]}
```

- `distance` is cosine **distance** (lower = more similar), ascending.
- `chunk_id` is the database primary key; the traceable corpus id
  (`BUD-…::COMP-…`) travels inside `metadata`.
- **422** — `k` outside `1..50`, empty `query`.
- **200 with `results: []`** for an empty corpus. Not a 404, not a 500.
- **500** — the same two generic bodies as ingest.

Both routes are unversioned (`/embeddings/ingest`, `/search`), joining the
exception already documented for the Session-7 path, and both declare a
`response_model`.

### Schema

`documents`: `id BigInteger PK`, `source_path Text NOT NULL UNIQUE`,
`document_type String(50) NOT NULL`, `ingested_at timestamptz NOT NULL
default now()`, `metadata JSONB NOT NULL default '{}'`.

`chunks`: `id BigInteger PK`, `document_id BigInteger FK → documents.id
ON DELETE CASCADE`, `chunk_type String(50) NOT NULL`, `content Text NOT NULL`,
`embedding Vector(1536) NULL`, `metadata JSONB NOT NULL default '{}'`,
`created_at timestamptz NOT NULL default now()`.

Indexes: `ix_documents_source_path` (unique), `ix_chunks_document_id`,
`ix_chunks_chunk_type`, `ix_chunks_metadata_gin` (GIN). **No index on
`embedding`.**

## Verification

Implemented 2026-10-24. Evidence:

- **Hermetic suite** — `uv run pytest -q` → `639 passed, 18 skipped,
  1 warning in 10.07s`. The 18 skips are the opt-in Postgres modules
  (10 ingestion, 8 vector store).
- **With the real database** —
  `$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator";
  uv run pytest -q` → `613 passed, 1 warning in 12.59s`.
- **Migration on Postgres** — `alembic upgrade head` against the compose
  database, then `\d documents` / `\d chunks`:
  `embedding | vector(1536)` nullable, `metadata | jsonb NOT NULL`,
  `chunks_document_id_fkey ... ON DELETE CASCADE`,
  `ix_documents_source_path UNIQUE`, `ix_chunks_metadata_gin gin (metadata)`.
  **No index covers `embedding`.**
- **Migration on SQLite (dialect guard)** — `alembic upgrade head` against a
  temp `sqlite:///...db` succeeds and creates only `alembic_version`,
  `ingestion_jobs`, `pseudonym_mappings`; the vector tables are skipped.
  `tests/test_ingestion_persistence.py` and `tests/test_ingestion_postgres.py`
  → `26 passed`.
- **Schema introspection** — `tests/test_rag_store_models.py` (7 tests):
  `metadata_` → `"metadata"`, nullable `Vector(1536)`, CASCADE FK, no vector
  index, exact index set, unique `source_path`, `RagBase` isolation, and
  `Base.metadata` compiles for SQLite.
- **Service and contract** — `tests/test_rag_ingest_service.py` (5),
  `tests/test_embeddings_api.py` (10), `tests/test_search_endpoint.py` (10),
  `tests/test_rag_database.py` (13): order of operations, duplicate guard,
  **rollback on embedder failure with no document written**, the dimension
  guard, the literal 409 body, the 422 matrix including the rejected Session-7
  batch payload, both generic 500s without leakage, `k` bounds, an empty corpus
  returning `200 []`, and the URL-derivation rules.
- **Real Postgres integration** — `tests/test_rag_postgres.py` (8 tests):
  extension present, `udt_name = vector`, no vector index, ingest persists both
  tables, duplicate refused by the service **and** by the unique index,
  cosine ordering ascending with the same-axis document first, an empty corpus
  returning no rows, and `ON DELETE CASCADE` removing the chunks.
- **End to end** — `scripts/query_examples.py` against the running API with a
  real `OPENAI_API_KEY`: `17 budgets — 17 ingested, 0 already present`; a
  second run reported `0 ingested, 17 already present` (idempotent); the five
  query angles returned ascending distances (direct match `0.4108` … `0.5258`,
  out-of-domain `0.5691` …, specific `0.5617` …). Recorded verbatim in
  `output_examples.txt`. Database after the run: **17 documents / 60 chunks**.
- **Distance parity** — the same five queries against the same corpus with the
  same model reproduce the reference project's distances to four decimals
  (`0.4108`, `0.4760`, `0.4982`, `0.5122`, `0.5258`), which is the strongest
  available evidence that the chunk text and the embedding path match.

Not verified: the sequential scan's performance at scale (there is no index and
no benchmark; the live session measures that), and behaviour under concurrent
identical ingests (the unique-index path is exercised, the race itself is not).

## Follow-ups

- Retrieval inside the estimation prompt (a later session; the composition
  belongs in `EstimationService`).
- HNSW/IVFFlat plus metadata filters and hybrid search — live-session work,
  measured against this sequential-scan baseline.
- Asynchronous "insert now, embed later" ingestion, which the nullable
  `embedding` column already permits.
- A re-embedding procedure for changing `EMBEDDING_MODEL`.
