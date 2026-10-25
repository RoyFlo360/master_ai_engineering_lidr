# 010 — Session 7: embedding pipeline (structural chunking → vectors over HTTP)

- **Status:** Implemented
- **Date:** 2026-10-18
- **Scope:** `app/embedding_pipeline/`, `app/dependencies.py`, `app/main.py`,
  `scripts/compare.py`, `data/budgets_sample.json`, `pyproject.toml`,
  `Dockerfile`, `tests/test_embedding_*.py`, `tests/test_embeddings_api.py`,
  `README.md`, `AGENTS.md`, `docs/`, `rules/`
- **Supersedes / Superseded by:** —

> **Corpus note (2026-10-24).** The sample corpus grew from 15 budgets / 52
> chunks to **17 budgets / 60 chunks (5,970 tokens)** when the reference project
> appended `BUD-2024-016` and `BUD-2024-017` in its session-08 commit. Every
> figure below is the **Session-7 reading**, left exactly as recorded. Current
> counts live in `README.md` and `docs/operations.md`; the ingest contract
> itself changed in spec 012.

## Problem

Session 6 ended with historical budgets cleaned and normalized as JSON, but there
is no way to turn them into vectors. Nothing in the service can answer "which
past project is most similar to this one" because no representation of past
projects exists outside the raw text.

The Session-7 brief (`instructions_session7.md`) asks for the first step of
semantic search: a pipeline that receives normalized budget documents, splits
them along their JSON structure, embeds each chunk with OpenAI, and returns the
vectors. Persistence and retrieval are explicitly deferred.

The reference implementation ships this untested. Our suite is hermetic and the
brief requires unit, integration, performance and security coverage, so the
tests are as much a deliverable as the code.

## Goals

- `POST /embeddings/ingest` accepts `{"budgets": [...]}` and returns one
  embedded chunk per budget component, plus aggregate stats.
- Chunking preserves structure: one component = one chunk, with the parent
  budget's context prepended so a component never loses which client, sector and
  technology it belongs to.
- Embedding is batched (100 chunks per API call), retries only rate limits
  (1s/2s/4s), and reports tokens and estimated cost.
- `scripts/compare.py` computes cosine similarity for two texts using only the
  standard library.
- Three recorded sanity pairs demonstrate the embeddings separate related from
  unrelated text.
- All four test categories exist and the default suite stays network-free.

## Non-goals

- Vector persistence (pgvector), retrieval, search, reranking.
- Alternative chunking strategies (recursive, semantic, hierarchical, late,
  contextual retrieval) and LLM chunk enrichment.
- Embedding-model comparison and Matryoshka dimension trimming.
- Normalizing the signed-budget corpus (`data/seed/budgets/`) into this schema.
- Any change to the estimation, streaming, session, ACB, cache, guardrail or
  attachment flows.
- Auth, rate limiting, a UI, or new application settings.

## Design

**Package.** `app/embedding_pipeline/` exactly as the brief prescribes —
`schemas.py`, `chunker.py`, `embedder.py`, `router.py`, `__init__.py`, plus
`SANITY_CHECK.md`. This is a deliberate exception to our layered convention
(`app/schemas/`, `app/services/`, `app/routers/`): the brief names the tree, the
live session builds on it, and `scripts/compare.py` imports from it. The
exception is recorded in `rules/development.md`.

**Schemas** (`schemas.py`). `Sector` and `Complexity` are `Literal`s so a typo
fails loudly. `Budget` carries `budget_id`, `client_metadata`,
`project_summary`, `main_technology`, `year`, `total_estimated_hours`, and
`components`. `Chunk` carries `chunk_id`, `text`, `metadata`, `token_count`;
`EmbeddedChunk` extends it with `embedding`. `IngestStats` is typed (not a bare
dict) so the contract is documented and validated. Request models use
`extra="forbid"`.

**Chunker** (`chunker.py`). `JSONStructuralChunker.chunk(budgets) -> list[Chunk]`.
The text is exactly the brief's template:

```
[Project: {project_summary}]
[Client sector: {sector} | Year: {year} | Main tech: {main_technology}]

Component: {name}
Description: {description}
Tech stack: {", ".join(tech_stack)}
Complexity: {complexity}
Estimated hours: {hours}
```

`chunk_id` is `{budget_id}::{component_id}`; metadata carries `budget_id`,
`component_id`, `client_sector`, `main_technology`, `year`, `complexity`,
`estimated_hours` and is **not** embedded. `token_count` comes from
`tiktoken.encoding_for_model("text-embedding-3-small")`, resolved lazily and
cached — a module-level load would make test collection depend on a network
fetch in a clean environment. No overlap, no fixed-size splitting.

**Embedder** (`embedder.py`). `OpenAIEmbedder(client, model)` with
`embed_one(text)` and `embed_many(chunks)`. Constants: `BATCH_SIZE = 100`,
`PRICE_PER_MILLION_TOKENS_USD = 0.02`, `_RETRY_BACKOFF_SECONDS = (1, 2, 4)`,
`EMBEDDING_DIM = 1536`. Only `RateLimitError` is retried; everything else
propagates. After each batch the vector count is checked against the chunk count
and a mismatch raises — the reference's `zip` would silently drop chunks. One
structlog event per batch with `chunks`, `tokens`, `latency_ms`, `model`; never
the text.

**Router** (`router.py`). `APIRouter(prefix="/embeddings", tags=["embeddings"])`,
`POST /ingest`, `response_model=IngestResponse`. Sequence:
`chunker.chunk(...)` → `embedder.embed_many(...)` → assemble stats.

**Wiring.** `get_chunker()` is a stateless singleton; `get_embedder()` reuses
`get_openai_client()` and returns `None` without an OpenAI key. Both are
`lru_cache`d and lazy.

**Hardening (decided in the plan, Q7–Q9).** Caps: ≤100 budgets per request,
≤100 components per budget, ≤500 chunks per request, ≤20,000 characters per
description, ≤500 per name, ≤50 tech-stack entries. Duplicate `budget_id`s, and
duplicate `component_id`s within a budget, are rejected so `chunk_id` is unique.
Violations are 422.

**Alternatives rejected.** *Unversioned vs `/api/v1`*: the brief mandates
`/embeddings/ingest`; an alias was rejected because two paths doubles the
surface to test and document. *Plain dict stats*: rejected because it loses
validation and OpenAPI documentation. *Eager tokenizer*: rejected because it
makes collection network-dependent.

## Contract

```
POST /embeddings/ingest
{"budgets": [<Budget>, ...]}
→ 200 {
    "chunks": [{"chunk_id": "BUD-2024-001::AUTH-001", "text": "...",
                "metadata": {...}, "token_count": 78, "embedding": [1536 floats]}],
    "stats": {"total_budgets": 15, "total_chunks": 52,
              "total_tokens": 4100, "estimated_cost_usd": 8.2e-05}
  }
```

- **422** — Pydantic validation: bad enum, out-of-range year/hours, empty
  `budgets` or `components`, unknown field, cap exceeded, duplicate identifier.
- **500** — generic `{"detail": "Embedding service is not available."}` when no
  OpenAI key is configured; generic `{"detail": "Failed to generate embeddings."}`
  for any embedding-API failure. Details go to the log, never the response.

`GET /docs` lists the route.

## Verification

Implemented 2026-10-18. Evidence:

- **Unit, integration, performance and security tests** —
  `\.venv\Scripts\python.exe -m pytest tests/test_embedding_schemas.py
  tests/test_embedding_chunker.py tests/test_embedding_embedder.py
  tests/test_embeddings_api.py tests/test_embedding_performance.py
  tests/test_embedding_security.py -q` → `80 passed, 1 warning in 0.93s`.
- **Whole suite** — `\.venv\Scripts\python.exe -m pytest -q` →
  `341 passed, 1 warning in 7.57s` (baseline was 261; no regression).
- **Real endpoint call** over `data/budgets_sample.json`, through `TestClient`
  and the real embedder:
  `status 200`;
  `stats {'total_budgets': 15, 'total_chunks': 52, 'total_tokens': 4126,
  'estimated_cost_usd': 8.252e-05}`;
  52 chunks, 1536 dimensions, first chunk `BUD-2024-001::AUTH-001`.
- **Sanity check** (`scripts/compare.py`, real API): Pair A **0.5957**,
  Pair B **0.1920**, Pair C **0.5407** — recorded in
  `app/embedding_pipeline/SANITY_CHECK.md`. Pair A sits just below the
  brief's 0.6 rule of thumb; the file says so instead of rounding it up.
- **OpenAPI** — `tests/test_embeddings_api.py::test_route_is_documented_in_openapi`
  asserts `/embeddings/ingest` is present in `/openapi.json`.
- **Dependency gate** — the full stack (including `tiktoken`) resolves and
  installs on Python 3.14.3; `uv lock` reported 142 packages, no fallback to 3.12.
- **Container verification (2026-10-18).** `docker compose build api` succeeds,
  the service boots, and the live stack answers:
  `GET /health` → 200;
  `POST /embeddings/ingest` with the 15-budget sample → 200 with
  `{"total_budgets": 15, "total_chunks": 52, "total_tokens": 4126,
  "estimated_cost_usd": 8.252e-05}`, 52 chunks, 1536 dimensions.
  `docker compose exec api python scripts/compare.py …` prints
  `Cosine similarity: 0.5957`, matching the host run.
  The full suite inside the container: **544 passed, 10 skipped** (the skips are
  the opt-in Postgres module); with `TEST_DATABASE_URL` pointed at the compose
  Postgres: **554 passed, 0 skipped**.

Not verified: behaviour against a non-OpenAI embedding provider — `token_count`
is only exact for the OpenAI `text-embedding-3-*` tokenizer.

## Follow-ups

- Persist vectors and add retrieval (later session, pgvector).
- Alternative chunking strategies (the live session).
- The tokenizer is exact only for OpenAI `text-embedding-3-*`; changing
  `EMBEDDING_MODEL` to another provider invalidates `token_count`.
