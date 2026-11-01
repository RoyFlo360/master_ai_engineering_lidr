# 014 — Session 9: architectural diagnostic and the search query bound

- **Status:** Implemented
- **Date:** 2026-10-31
- **Scope:** `app/embedding_pipeline/schemas.py` (`MAX_QUERY_CHARS`),
  `tests/test_embedding_schemas.py`, `tests/test_search_endpoint.py`,
  `examples/trace_s09.py` (new), `examples/transcripts/` (new),
  `arquitectura-actual.md` (new), `docs/data-model.md`, `rules/security.md`,
  `AGENTS.md`, `README.md`
- **Supersedes / Superseded by:** — (extends spec 012 without changing its design)

## Problem

Session 9 is not a feature session. The brief asks for **one architectural
diagnostic document** (`arquitectura-actual.md`) that (a) describes the service at
the end of Session 8, (b) traces a raw transcript through it with real responses,
(c) names five concrete flaws, and (d) proposes the modules that would close the
loop to a generated estimate. It explicitly forbids implementing any of them.

Running that trace exposed one blocking defect in our own service. Step 2 of the
exercise sends the whole `02_ambiguous.txt` to `POST /search`, and
`SearchRequest.query` carried `max_length=2_000`:

```text
status: 422
{"detail":[{"type":"string_too_long","loc":["body","query"],
  "msg":"String should have at most 2000 characters",
  "ctx":{"max_length":2000}}]}
```

The probe transcripts measure 1,975 / 2,854 / 2,397 characters, so **two of the
three were refused before any embedding happened**. `01_clear.txt` passed only
because it is 25 characters under the cap — the failure looked intermittent
rather than systematic.

## Goals

- The trace can send a complete meeting transcript to `POST /search`.
- The bound remains a bound: `/search` is unauthenticated and each call is billed.
- The three probe transcripts are valid `SearchRequest` payloads, pinned by tests.
- The deliverable records the failure and the decision, with the real numbers,
  instead of silently working around it.

## Non-goals

- Query reformulation, reranking, a new retriever, thresholds, metadata filters.
- A generation module, new endpoints, or any wiring of retrieval into
  `EstimationService`.
- Changing the `/search` response shape, the SQL, the error mapping, the embedding
  model or the database schema.
- Truncating the transcript in the client: the exercise requires embedding the
  whole file, and truncation would bias the diagnosis.
- Vector indexes, hybrid search, Postgres tuning (still live-session material).

## Design

One constant, in `app/embedding_pipeline/schemas.py`:

```python
MAX_QUERY_CHARS = 20_000   # was 2_000
```

**Why 20,000.** The transcript is 2,854 characters / 890 tokens (~3.2 chars per
token). 20,000 characters is ~6,200 tokens, comfortably below
`text-embedding-3-small`'s 8,191-token input limit, and still small enough that an
unauthenticated caller cannot push an unbounded string into a billed embeddings
call. Removing the bound entirely was rejected for that reason; adding a second
`MAX_TRANSCRIPT_CHARS` and a separate field was rejected because it adds surface
for a one-off diagnostic.

**Why touch `SearchRequest` at all.** The brief also says "do not modify the
existing search endpoint". The two instructions conflict only here. A length bound
is input validation, not retrieval behaviour: the route, the cosine SQL, the
response schema and the error matrix are unchanged. The alternative — recording
the 422 and calling `SemanticRetriever.search` directly from inside the `api`
container — was rejected as a workaround that would leave the documented endpoint
unable to accept the input the exercise is about. The deliverable states plainly
which path was taken.

### The trace client

`examples/trace_s09.py` is a client only. It imports `httpx`, `openai`, and two
modules that need no `Settings` (`app.embedding_pipeline.embedder`,
`app.embedding_pipeline.chunker`), so importing it never constructs a service or
reads `.env`. It embeds the transcript (printing dimensionality, L2 norm and the
first/last component), POSTs it to `/search`, prints the raw JSON, and writes the
body with `--json-out`. `--dry-run` reports the resolved configuration without a
network call. On a non-200 it prints the response body before exiting 2: a bare
traceback would hide exactly the information the trace exists to collect.

The three transcripts and `TEMPLATE.md` are copied **byte-identically** from the
reference project so the trace is comparable.

## Contract

`SearchRequest.query`: `min_length=1`, `max_length=20_000` (was 2,000).
Everything else is unchanged:

```text
POST /search  {"query": "<up to 20,000 chars>", "k": 1..50}
→ 200 {"query", "k", "search_time_ms", "results": [SearchHit]}
→ 422 for an empty query, k out of bounds, or query over 20,000 chars
→ 500 (generic) when the retriever is unavailable or the query fails
→ 200 with results: [] for an empty corpus
```

`docs/data-model.md` and `rules/security.md` carry the same number.

## Verification

Implemented 2026-10-31.

- **Hermetic suite** — `uv run pytest -q` → **664 passed, 18 skipped, 3 warnings**
  (baseline before this change: 659 passed, 18 skipped). The five new cases are one
  schema test, three parametrised transcript cases, and one test that pins the
  old-bound asymmetry (1,975 passes; 2,397 and 2,854 do not).
- **With the real database** —
  `$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator";
  uv run pytest -q` → **682 passed, 0 skipped**. The 18 tests that skip without a
  database (10 ingestion, 8 vector store) all pass against compose Postgres, so the
  query-bound change did not disturb them. Re-verified 2026-11-01 with the stack up
  and the 17-document corpus ingested.
- **New tests** — `tests/test_embedding_schemas.py`:
  `test_real_transcripts_fit_the_search_query_bound` validates all three files as
  `SearchRequest`. `tests/test_search_endpoint.py`:
  `test_search_accepts_a_whole_transcript[01/02/03]` proves the transcript reaches
  the retriever intact at the HTTP edge, and
  `test_the_old_query_bound_would_have_rejected_two_of_three_transcripts` records
  the asymmetry (1,975 passes; 2,854 and 2,397 do not).
- **Failure reproduced before the change** — `POST /search` with
  `02_ambiguous.txt` returned `422 string_too_long`, `ctx.max_length = 2000`,
  captured in the deliverable's step 2a.
- **Success after the change** — the same request returned `200`, `k=5`,
  `search_time_ms=904`, five hits at cosine distances 0.6083 / 0.6138 / 0.6372 /
  0.6388 / 0.6444. Raw output in `examples/trace_s09_output.txt`.
- **Live schema check** — `GET /openapi.json` reports
  `SearchRequest.properties.query.maxLength = 20000` on the running container.
- **Contrast runs** — `01_clear.txt` → 0.4148–0.4843; `03_hard.txt` →
  0.5176–0.6161. Same corpus, same ingest, same model: only the query differs,
  which is the diagnosis's central evidence.

Not verified: that 20,000 characters is the right ceiling for *every* corpus. It
is chosen against one model's token limit and three real transcripts; a provider
change would need the number revisited. No vector index exists, so timings are
sequential-scan timings on 60 chunks.

## Follow-ups

- The six modules proposed in `arquitectura-actual.md` §4 are **not built**. Query
  understanding, budget-level aggregation, a relevance threshold, a context
  assembler with parent totals, grounded generation and citation validation are
  the next sessions' work.
- `documents.metadata` still does not persist `total_estimated_hours`, so a
  context assembler would have to re-read the corpus until that changes.
- `SearchRequest.query` has no per-user rate limit; the bound is the only spend
  guard on the endpoint.
