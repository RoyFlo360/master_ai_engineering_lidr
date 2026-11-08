# Session 10 plan — hybrid search + cross-encoder reranking

- **Status:** DRAFT — **every open question is decided** (C1 `english`, C2 split
  package at the brief's path, C3 T0 + T0B, Proposal A + `/search` retirement,
  the report file, the mutation script, the golden set, and fail-closed 502 on
  reranker failure). No code has been changed.
- **Sources:** `docs/instrucctions-missing/instructions_session10.md` (the brief)
  and `docs/instrucctions-missing/MISSING-session10.md` (the gap analysis).
- **Baseline:** branch `session-10-2026-09`, HEAD `5edcbe0`, `uv run pytest -q` →
  **665 passed, 19 skipped, 3 warnings in 42.97s** (2026-11-07).
- **Reference:** `ai-engineering-2/ai-engineering`, `origin/session_10`
  (`ff6b16b`) for the pre-work answer, `origin/session_10_live` (`dd345ed`) for
  what the live class adds. `estimator-web/` ignored throughout.

---

## 1. What the brief settles, and what it leaves open

The brief resolves five of the seven decisions the gap analysis raised:

| Decision | Resolved by the brief | How |
| --- | --- | --- |
| D1 — scope | **Yes** | "This exercise covers only hybrid search and reranking. Do not implement query expansion, multi-index routing, or metadata filtering." |
| D4 — reranker | **Yes** | A pre-built wrapper is provided; we integrate it, recall-50 → rerank-5, and it must be switchable "without modifying the code". No stub. |
| D6 — live material | **Yes** | Explicitly forbidden: query expansion, multi-index routing, metadata filtering all out. |
| D7 — language | **Yes** | "all code, names, comments, and log messages must be in English." |
| D3 — Postgres scope | **Inferred** | Not mentioned; keep our convention: Postgres-only DDL, migration returns early on SQLite (mirrors `0002_rag_pgvector`). |

Still open — and the brief *requires* an answer before the work starts:

| Decision | State | Our recommendation |
| --- | --- | --- |
| D2 — Track 0 width | The brief assumes the Session-9 RAG pipeline already exists. It does not. | Build the **retrieval core** (T0), defer the generation chain (T0B) unless you say otherwise. |
| D5 — runtime toggles | The brief only needs "enable/disable without modifying the code". | `.env` settings + request overrides. Redis runtime store only if you want live flipping. |

The brief also adds requirements the gap analysis did not anticipate: **mutation
testing**, **non-functional tests** (security, maintainability, performance), a
**5-query golden set**, a **four-configuration comparison table**, and a short
**conclusions paragraph**. Those are phases T4–T7 below.

---

## 2. Conflicts and discrepancies found while reading the brief

These are the places where the brief and the repository disagree. Each one is
answered in the plan with a recommendation, and each needs your confirmation.

### C1 — "The dataset entries are in Spanish" — they are not

The brief says: *"The dataset entries (project estimates) are in Spanish; keep
this in mind when selecting the text search configuration for the column."*

Verified on 2026-11-07:

- `data/budgets_sample.json` — the corpus that is chunked and embedded — is
  **English**: `BUD-2024-001` = *"Mobile banking API with OAuth 2.0 authentication
  and PSD2 compliance"*, components like *"OAuth 2.0 authentication backend"*.
- The chunk text template (`render_component_text`, `chunking/structural.py`)
  produces English prose: `[Project: …] [Client sector: … | Year: …]`.
- `data/seed/budgets/*.json` **is** Spanish (`BUDGET-2024-0001`, "Diseño UX",
  "Pruebas y despliegue") — but those files feed the Session-7 *ingestion* pipeline
  (`client_name` / `phases`), not the `Budget` schema that `/embeddings/ingest`
  accepts. They are never embedded.
- The course's own reference solution hit the same contradiction and recorded it
  in `0003_session10_fts.py`: *"the exercise statement says the corpus is in
  Spanish, but the shipped dataset … is actually in ENGLISH … We therefore use the
  `english` configuration."*

**Recommendation:** `to_tsvector('english', content)` and
`plainto_tsquery('english', $1)`, with the discrepancy documented in the migration
docstring and pinned by a test (§T5.1). The alternative — following the brief
literally with `'spanish'` — stems English words with Spanish rules and drops
Spanish stop-words that never appear; the index still builds, the recall quietly
drops. **If you would rather obey the brief literally, say so and we switch one
constant.**

**DECIDED (2026-11-07): `english`.** Verified that the reference's
`data/budgets_sample.json` at `origin/session_10` is English too — 0
Spanish-looking strings, the same 17 ids and the same `project_summary` values as
ours. The reference's own `0003_session10_fts.py` records the same finding.

**Can Spanish be added later?** Yes, at a known and bounded cost — it is a
migration, not a redesign:

- `content_tsv` is `GENERATED ALWAYS … STORED` with a literal regconfig, so the
  expression cannot be altered in place: adding Spanish means
  `DROP COLUMN` + re-`ADD COLUMN` with the new config, which **rewrites every
  row** and rebuilds the GIN index. One migration, one downtime window.
- The query side must change in the same commit (`plainto_tsquery`), or the index
  is bypassed.
- Mixing languages in one column needs one of: `'simple'` (no stemming, no
  stop-words — language-agnostic but weak), or a `regconfig` column read by the
  generated expression so each row is stemmed by its own config. Both are one
  migration each; neither is a rewrite of the pipeline.
- The live session's per-collection split (forbidden here) is the natural place
  for per-language config, since each table can carry its own.

So: choose `english` now, and record in the spec that switching is a
`DROP`/`ADD COLUMN` migration plus one query constant. Nothing else in the design
assumes one language.

### C2 — The pipeline path and the service name in the verification command

The brief says the wrapper lives at `app/generation/rag/retrieval/` and gives:

```bash
docker compose exec ai-service python -m app.generation.rag.retrieval.verify_reranker
```

Ours differ on both counts:

- We have no `app/generation/`. Our retrieval code lives in
  `app/embedding_pipeline/` (`retriever.py`, `search_router.py`, `store/`).
- Our compose service is named **`api`**, not `ai-service`. The working command is
  `docker compose exec api python -m …`.

**DECIDED (2026-11-07): option (b) — a split package, brief-compatible path.**

- **New home for the Session-10 modules only:**
  `app/generation/rag/retrieval/` containing `fusion.py`, `reranker.py`,
  `verify_reranker.py`, `pipeline.py`, `schemas.py` and a `README.md`.
  The brief's literal command then works unchanged:
  `python -m app.generation.rag.retrieval.verify_reranker`.
- **Existing code does not move.** `app/embedding_pipeline/` keeps `chunker`,
  `chunking/`, `embedder`, `ingest_service`, `retriever`, `search_router`,
  `store/`. Zero of the 133 import lines change; no test breaks.
- **One README marks the boundary.** `app/generation/rag/retrieval/README.md`
  states: this package is the *query-time ranking half* of RAG (lexical search,
  fusion, reranking, the four configurations); `app/embedding_pipeline/` is the
  *corpus half* (chunking, embedding, ingest, vector store, `/search`); and it
  names which module owns what. It is the single place a reader is told the
  system is deliberately divided in two.
- **`AGENTS.md` points at both halves.** Add a short "RAG, two packages" note to
  the Layout section: `app/embedding_pipeline/` (corpus: chunk → embed → store →
  `/search`) and `app/generation/rag/retrieval/` (query: lexical + vector → RRF →
  rerank → `/v1/retrieval/search`), with a line saying the split is intentional
  and that neither half imports the other's internals — they meet at
  `ChunkStore` and at the `RetrievedChunk` contract.
- **Service name:** still a deviation. Our compose service is `api`;
  `docker compose exec ai-service …` becomes `docker compose exec api …`. Record
  it in the spec and in the README.

Why not (c) — moving the whole RAG layer: it touches 133 import lines across 19
files for a naming preference, and it would put chunking and ingestion under a
`generation/` prefix that describes neither.

### C3 — "The RAG pipeline from the previous session, fully functional"

We do not have it. Session 9 in this repo was the **pre-work only**: a diagnosis
document, three transcripts and a trace client (spec 014), plus the HNSW index
(spec 015). The reference's `session_09_live` layer — 46 files, +6,198/−11 — was
never implemented; `MISSING-session9.md` §7 deferred it and it stayed deferred.

That layer includes the reformulator, context assembler, grounded estimator,
citation validation, idempotency, and the `/v1/retrieval/search`,
`/v1/estimate/from-transcript` and `/v1/estimate/stages/*` routes.

**Recommendation:** split it:

- **T0 — retrieval core (required).** `RetrievedChunk`, `RetrievalResult`, a
  `retrieve()` returning graded chunks, an optional `distance_threshold`, and one
  HTTP surface for the four configurations. This is what steps 1–4 actually need:
  the reranker's input contract, the grading key, and a reproducible way to invoke
  the four configurations.
- **T0B — generation chain (optional, separately spec'd).** Reformulator →
  assembler → grounded estimate → citation validation → idempotency. ~2–3 days,
  and it contributes nothing to the graded table. The brief's "fully functional"
  bullet arguably wants it; steps 1–5 do not.

**No metadata filters in T0.** The brief explicitly forbids metadata filtering, so
the reference's `search_filtered(sectors=…, project_year_min=…)` is out. T0 adds
only `distance_threshold` (a relevance floor, not a metadata filter).

### C4 — `/search` is retired, not kept as a second contract (decided 2026-11-07)

`/search` is **not** dead code today: it has five live dependents, all verified:

| Caller | Uses | Effect of deleting |
| --- | --- | --- |
| `examples/trace_s09.py:157` | `POST /search`, reads `results[]`, `search_time_ms` | Session-9 graded artifact — must be migrated |
| `scripts/query_examples.py:116` | `POST /search`, reads `results[]` | Session-8 smoke test — must be migrated |
| `tests/test_search_endpoint.py` | route contract | rewritten against the new route |
| `tests/test_embedding_schemas.py`, `tests/test_rag_postgres.py` | `SearchRequest`/`SearchHit` | keep the models, retarget the assertions |
| `specs/014-…md`, `docs/architecture.md`, `docs/data-model.md`, `docs/data-flows.md` | the route name | amended prose, new spec note |

**Decision:** build `/v1/retrieval/search` in T0–T2, prove it, then in **T6** delete
`search_router.py`, migrate the two scripts and the tests, and amend spec 014 with
a short "superseded by 016 for the retrieval surface" note. Nothing is deleted
until the replacement has passed the four-configuration run — that keeps the
rollback cheap.

**What does not move:** `SemanticRetriever`, `ChunkStore.search`, `OpenAIEmbedder`,
the HNSW index, `SearchRequest`/`SearchHit` as *models* (they may stay for the
script-level callers, or be replaced by `RetrievalRequest`/`RetrievedChunk` —
decided in T0 when the response shape is fixed). The route file is the only thing
that necessarily disappears.

**What is reused, not rewritten (verified):** `search_router.py` holds no search
logic — it validates, checks `get_semantic_retriever()`, and calls
`SemanticRetriever.search` (`search_router.py:39`). That method calls
`ChunkStore.search` (`retriever.py:45`). So `pipeline.py` calls the *same two
methods* for its vector branch, and the deletion removes only: the router file,
`app/main.py:9` (import) and `app/main.py:74` (`include_router`). No SQL is
duplicated, and there is no second place where the vector query is written.

**Risk to state in the spec:** spec 014 is `Implemented` and its verification
section cites `/search`. Retiring a route an implemented spec promises is a
contract change and needs the note, not a silent deletion.

### C5 — Deliverable destinations are not stated

The brief asks for a "comparison table" and a "conclusions paragraph" but names no
file. Our precedent is a root `REPORT.md` (Session 6 stress) and a root
`arquitectura-actual.md` (Session 9). **Recommendation:** one root file,
`session10_report.md`, holding the golden set, the four-configuration table, the
method notes and the conclusions; the golden set itself as
`evals/golden_retrieval.json`; the harness as `scripts/eval_retrieval_s10.py`.

---

## 3. Two proposals for step 2 — choose one

The brief asks for exactly two options for *module structure, RRF parameterization
and search-mode exposure*, with an argument for the winner.

Both proposals share the same internal pieces, in the path C2 settled on
(`app/generation/rag/retrieval/`, the query-time half; the corpus half stays in
`app/embedding_pipeline/`):

```
app/generation/rag/retrieval/
    README.md        names the split: query-time ranking here, corpus in embedding_pipeline
    __init__.py
    fusion.py        reciprocal_rank_fusion(rankings, *, k)   # pure, no I/O
    reranker.py      CrossEncoderReranker                      # provided wrapper
    verify_reranker.py                                         # pre-flight gate
    pipeline.py      retrieve(...)                             # the four configs
    schemas.py       RetrievedChunk, RetrievalResult, RetrievalRequest
    router.py        POST /v1/retrieval/search
```

`pipeline.py` imports `ChunkStore` and `OpenAIEmbedder` from
`app.embedding_pipeline`; nothing in `app/embedding_pipeline` imports back. That
one-way edge is the whole of the contract between the two halves, and the README
says so.

They differ in **where the four configurations are exposed** and **how `k` is
parameterized**.

### Proposal A — new module + a versioned retrieval endpoint (reference-shaped) — **CHOSEN**

- `POST /v1/retrieval/search` with
  `{query, top_k=10, recall_k=50, search_mode="vector"|"hybrid", rerank=false,
  rrf_k: int|null, distance_threshold: float|null}`.
- `RRF_K: int = 60` in `Settings` (`ge=1`), overridable per request via `rrf_k`
  (also `ge=1`, so `422` not `500` on a bad value).
- **`/search` is deleted once the new route is proven** (T6), its two script
  callers migrated to the new path — see C4. One retrieval contract, not two.
- The eval CLI drives the same `retrieve()`; the table and the HTTP path cannot
  drift.

**Pros**
- One entrypoint for all four configurations, reachable from tests, a script, and
  an HTTP client — the brief's "reproducible way" is unambiguous.
- One published retrieval contract after T6; no route that silently differs from
  the pipeline beside it (exactly the reference's defect, caveat #6).
- Matches the course's artefacts, so the grader's expectations (paths, payload
  field names) are met with minimal translation.
- `RetrievedChunk`/`RetrievalResult` is exactly what T0 needs anyway; the route is
  a thin shell over it.

**Cons**
- A second retrieval route beside `/search`; two surfaces to document.
- New request/response schemas and their bounds.
- Needs a decision on auth: our repo has none anywhere, the reference requires
  `X-API-Key` on this route. Adding auth to one route only would be inconsistent.

### Proposal B — no new route; extend `/search` with three optional fields

- `SearchRequest` gains `search_mode: "vector"|"hybrid" = "vector"`,
  `rerank: bool = false`, `recall_k: int = 50` (all defaulted to today's
  behaviour); `SearchResponse` gains the fused list plus `search_time_ms` as now.
- `RRF_K` lives only in `Settings`; no per-request override.
- The eval CLI imports `retrieve()` directly.

**Pros**
- Smallest possible surface: no route, no new schemas beyond the internal ones,
  no auth question.
- Existing clients keep working: omitting the new fields reproduces today's
  vector-only, no-rerank behaviour exactly.
- Fastest path to the graded table.

**Cons**
- Grows the contract of an endpoint spec 014 declared stable, and makes
  `SearchResponse` carry two different result semantics depending on a flag.
- Four configurations are only reproducible through HTTP parameters or the CLI —
  there is no named, self-describing surface a reviewer can point at.
- If T0B ever lands, it needs a retrieval route anyway, so the new route appears
  later and `SearchRequest` has to be un-grown or kept as a second path.

### Decision: **Proposal A**, with `/search` retired in T6 (2026-11-07)

1. **It satisfies the brief's reproducibility clause structurally.** A route with
   explicit `search_mode`/`rerank` fields plus a settings default is three named
   ways to invoke the same configuration; a grown `/search` is one way with two
   implicit behaviours.
2. **It avoids a permanent second contract.** Keeping both routes would reproduce
   the reference's caveat #6 (two retrieval paths, two behaviours): `/search` is
   vector-only and would silently ignore the new switches. Retiring it in T6
   leaves one contract.
3. **T0 already needs the types.** `RetrievedChunk` and `RetrievalResult` are
   unavoidable (the reranker's `text_of`, the grader's `source_id`). Once they
   exist, the route is ~40 lines of transport.
4. **It matches what the grader will compare against.** The reference exposes the
   four configurations on `/v1/retrieval/search`; a reviewer looking for parity
   will find it.

**Cost of the deletion, paid in T6:** two scripts, three test files and four docs
— inventoried in C4. Nothing is deleted before the new route passes the
four-configuration run.

**Choose B instead if** you want the smallest diff and do not care about an HTTP
surface — it is genuinely less code, and the graded table would be identical.
**Rejected third option:** a CLI-only surface (`--search-mode` / `--rerank`
flags) with no HTTP at all. It satisfies the letter of the brief and nothing else:
no client can use it, and the "enable/disable without modifying the code" clause
becomes a command-line flag nobody outside the evaluation can set.

---

## 4. Work breakdown

Ordered, with the dependency graph in mind: T0 → T2 → T3 → T5 depend on each
other; T1 and T4's golden set are independent.

### T0 — Retrieval core (the Session-9 prerequisite), ~1–1.5 days

**Deliverables**

- `app/generation/rag/retrieval/schemas.py`
  - `RetrievedChunk`: `chunk_id`, `document_id`, `budget_id` (from
    `metadata["budget_id"]`), `component_id`, `chunk_type`, `content`, `sector`,
    `project_year`, `distance`, `rank` (optional), `source` (`"vector"` |
    `"lexical"` | `"fused"`).
    `source` fixes the reference's caveat #2 — its `distance` carries two
    meanings with no way to tell them apart.
  - `RetrievalResult`: `chunks`, `search_mode`, `rerank`, `candidates_evaluated`
    (vector **and** lexical counts, caveat #1), `search_time_ms`,
    `low_confidence`.
- `app/generation/rag/retrieval/README.md` — the boundary note described in C2:
  query-time ranking here, corpus in `app/embedding_pipeline/`, the one-way import
  edge, and the `api` service-name deviation. Written in T0, before the modules,
  so T2/T3 fill a documented package rather than inventing one.
- `ChunkStore.search(...)` gains an optional `distance_threshold` (no metadata
  `WHERE` — see C3) and returns a stable row shape including `metadata`.
- `SemanticRetriever.retrieve(...)` (or a new `RetrievalService`) assembles
  `RetrievedChunk`s from rows, embedding the query once.
- `POST /v1/retrieval/search` (Proposal A) — thin transport in
  `app/generation/rag/retrieval/router.py`, `response_model`, 422 on bounds, 500
  on unavailable embedder (matching `search_router.py`'s existing posture), no
  auth (consistent with the rest of our repo).

**Gate**

```powershell
uv run pytest -q
docker compose up -d
$body = '{"query":"mobile banking API with OAuth2","top_k":5,"search_mode":"vector","rerank":false}'
Invoke-RestMethod -Method Post -Uri http://localhost:8000/v1/retrieval/search -ContentType 'application/json' -Body $body
```

Expected: five chunks, `budget_id` present, `source: "vector"`.

**Out of scope here:** reformulator, context assembler, grounded generation,
citation validation, idempotency, API keys, rate limiting, metadata filters.

### T0B — Generation chain (approved: in scope), ~2–3 days

The remaining Session-9 live layer: `query_reformulator.py`,
`context_assembler.py`, `estimator.py`, `validation.py`, `idempotency.py`,
`observability.py`, `/v1/estimate/from-transcript`, `/v1/estimate/stages/*`.
It is what makes the pipeline "fully functional" in the brief's first bullet, and
it changes nothing in the graded table. **Decision (2026-11-07): in scope.**

### T1 — Full-text column and GIN index, ~0.25 day

**Deliverable:** `alembic/versions/0004_rag_fts.py`

```sql
ALTER TABLE chunks ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX ix_chunks_content_tsv ON chunks USING gin (content_tsv);
```

- The regconfig is a module-level constant, `FTS_REGCONFIG = "english"`, echoed in
  the DDL literal (see C1 — one constant, one test).
- `upgrade()` returns early on a non-Postgres dialect, exactly like
  `0002_rag_pgvector` (AGENTS.md gotcha 50), so a host `alembic upgrade head`
  against SQLite still creates the ingestion tables.
- `downgrade()` drops the index and the column.
- The ORM model gets nothing: `content_tsv` is Postgres-only and would break
  `Base.metadata.create_all()` in the SQLite test fixtures (gotcha 49). The
  lexical query targets the column by name, not through a mapped attribute.

**Gate**

```powershell
docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec postgres psql -U estimator -d estimator -c "\d+ chunks" | Select-String content_tsv
uv run pytest -q
```

### T2 — Lexical branch, RRF and the hybrid pipeline, ~0.75 day

**Deliverables**

- `ChunkStore.search_lexical(session, *, query_text, top_k=50)` —
  `plainto_tsquery(FTS_REGCONFIG, …)`, `ts_rank_cd`, `@@`, `ORDER BY rank DESC`,
  `LIMIT top_k`. No threshold (the reference's caveat #3 records what happens when
  one branch is thresholded and the other is not — we keep both untresholded for
  fusion and apply a threshold only to the final fused list, if at all).
- `retrieval/fusion.py::reciprocal_rank_fusion(rankings, *, k=60)` — pure,
  synchronous, deterministic tie-break on ascending id, `ValueError` on `k <= 0`.
  **1-based** positions (`1 / (k + rank)` with `rank` starting at 1), and the
  docstring says so — this is the reference's caveat #4, and we should not
  reproduce it.
- `retrieval/pipeline.py::retrieve(...)` — the four configurations:
  - vector / no rerank: `top_k` straight from the vector ranking;
  - hybrid / no rerank: vector `recall_k` + lexical `recall_k`, RRF;
  - vector / rerank: vector `recall_k` → reranker → `top_k`;
  - hybrid / rerank: both branches `recall_k` → RRF → reranker → `top_k`.
- `Settings`: `RRF_K=60` (`ge=1`), `RETRIEVAL_RECALL_TOP_K=50`,
  `RERANK_TOP_N=5`, `RERANKER_ENABLED=False`, `RETRIEVAL_SEARCH_MODE="vector"`,
  `RERANKER_MODEL="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"`.
- Route: `/v1/retrieval/search` accepts the four switches (Proposal A).

**Gate**

```powershell
uv run pytest -q tests/test_rrf_fusion.py tests/test_hybrid_retrieve.py
```

plus the Postgres-marked pair: a query containing a rare identifier
(`"BUD-2024-013"` or a component name) must return in the lexical top-5 a chunk
that the vector top-5 misses.

### T3 — Reranker integration, ~0.5–1 day

**Deliverables**

- `retrieval/reranker.py` — the provided wrapper, adapted: `from_settings()`,
  lazy `threading.Lock` load, `sentence_transformers` imported **inside** the
  loader (never at module import), `score(query, docs)`, `rerank(query,
  candidates, *, top_n, text_of=…)`.
- `retrieval/verify_reranker.py` — the pre-flight gate, exit 0/1/2, using a
  sanity pair.
- `Settings.RERANKER_MODEL`, and a `RERANKER_ENABLED` default of `False` (the
  brief requires switching without code changes, not on-by-default).
- `docker-compose.yml`: an `HF_HOME` volume so the weights survive a recreate
  (reference caveat #8), and the dev-mount list already covers `app/`.
- **Failure policy — DECIDED (2026-11-07): fail closed with 502.** When the
  cross-encoder cannot load or score, the route returns
  `502 {"detail": "Reranking service is unavailable. Please retry later."}` and
  logs `reranker_unavailable` with the error type. No silent fallback.
  Rationale: the caller asked for `rerank=true`; returning recall order with a
  200 would report a configuration that never ran — a false positive the eval
  harness would happily record as config C or D. The un-reranked paths
  (`rerank=false`, both modes) are unaffected and keep working, so a cold start
  degrades *the capability*, not the service.
- **The 502 must be caught before it looks like a retrieval failure.**
  `raise HTTPException(502, ...)` from the rerank step only; any other exception
  in `retrieve()` keeps its existing mapping (422 for bad bounds, 500 for
  unavailable embedder/store, matching `search_router.py`'s posture today).

**Gate (the brief's gate — run before writing any pipeline code)**

```powershell
docker compose up -d
docker compose exec api python -m app.generation.rag.retrieval.verify_reranker
```

Expected `OK: reranker loaded and ranked the relevant document first.` and exit 0.
**If this fails, stop and report** — the brief says so explicitly.

Environment risk, measured rather than assumed: `sentence-transformers>=3.0`
resolves on Python 3.14 (`uv pip install --dry-run` → `torch==2.15.0`,
`transformers==5.22.2`, `sentence-transformers==6.2.0`, 42 packages). Resolution
is not a load — `transformers` major 5 is newer than the reference's stack, so the
`CrossEncoder` API must be confirmed at the gate. If it has moved, pin
`transformers<5` and record the pin.

### T4 — Golden set, harness, four-configuration table, ~0.75 day

**Deliverables**

- `evals/golden_retrieval.json` — **5 queries**, each a project description to be
  estimated, each annotated with the `budget_id`s that are genuinely relevant.
  Verified: all five of the reference's annotated id sets exist verbatim in our
  corpus, and our 17-budget `project_summary` values are identical to the
  reference's. So Q1–Q5 can be adopted as-is; we still re-read each annotation
  against our corpus before committing it, because a copied label that is wrong is
  worse than no label.
- `scripts/eval_retrieval_s10.py` — `NamedConfig` A–D, `precision@5`, latency
  (1 warm-up + 3 measured runs), per-query breakdown, JSON + Markdown output.
  Grading on `budget_id` at ≤5 hits; ties and duplicates counted once.
- `session10_report.md` (root) — the golden set, the table, method notes, and §5's
  conclusions.

**Gate**

```powershell
docker compose up -d
docker compose exec api python scripts/query_examples.py          # idempotent ingest
$env:ESTIMATOR_API_BASE_URL = "http://localhost:8000"
uv run python scripts/eval_retrieval_s10.py
```

Expected: four rows, `A ≤ B` and `A ≤ C` on precision and `C/D` slower than `A/B`
by the reranker's cost. **If a row disagrees with that expectation, report it —
do not tune the golden set to fit the hypothesis.**

### T5 — Tests: unit, integration, mutation, non-functional, ~1 day

**T5.1 Unit (hermetic)**
- `tests/test_rrf_fusion.py` — score arithmetic (1-based), dedup within a ranking,
  empty branch, tie-break, `k <= 0`.
- `tests/test_reranker.py` — fake model object; ordering, `top_n`, `text_of`;
  assert `torch`/`sentence_transformers` are **not** imported by importing the
  module and checking `sys.modules`.
- `tests/test_hybrid_retrieve.py` — fake store returning fixed rows; all four
  configurations; `candidates_evaluated` counts both branches; lexical-only hits
  are labelled `source: "lexical"`.
- `tests/test_fts_regconfig.py` — parse `0004_rag_fts.py`, extract
  `to_tsvector('<x>'`, assert `x == FTS_REGCONFIG`. This is the test that makes
  C1's single-constant rule enforceable.

**T5.2 Integration (Postgres-marked, `TEST_DATABASE_URL`)**
- `search_lexical` returns rows for a rare identifier and none for a stop-word-only
  query.
- Migration round-trip: `upgrade head` then `downgrade -1` leaves `chunks` without
  `content_tsv` and the HNSW index intact.
- Route-level: `/v1/retrieval/search` with a fake retriever — 200 for all four
  combinations, 422 on `rrf_k=0`, 422 on `search_mode="semantic"`, 500 when the
  embedder is unavailable, **502 when the injected reranker raises**, with body
  `{"detail": "Reranking service is unavailable. Please retry later."}`, and
  **200 for `rerank=false` while that same broken reranker is wired** — the
  failure must not leak into the un-reranked paths.

**T5.3 Mutation testing (new for this repo)**
- Tool: `mutmut 3.8.1` (resolves on 3.14).
- Scope: `app/generation/rag/retrieval/fusion.py`,
  `app/generation/rag/retrieval/pipeline.py`,
  `app/embedding_pipeline/store/repository.py` (the new lexical function),
  `app/generation/rag/retrieval/reranker.py`.
- Threshold: **≥ 90% of mutants killed** in those files; the surviving mutants are
  listed in the report with a one-line justification each. Not run in the default
  `pytest` path — a `scripts/mutation_s10.ps1` wrapper plus a documented CI job.

**T5.4 Non-functional**
- **Security** (`tests/test_retrieval_security.py`): query-length cap still
  enforced; `rrf_k` bounds; no query text, chunk text or key material in error
  bodies or logs; `search_mode` is a closed literal (no arbitrary strings);
  reranker model name comes from settings, never from the request; unbounded
  `k`/`top_k` rejected before any embedding call (cost control).
- **Performance** (`tests/test_retrieval_performance.py`): property assertions in
  the style of `tests/test_embedding_performance.py` — exactly one embed call per
  request in both modes; RRF is O(n log n) (assert on a 5,000-id input without a
  wall-clock claim beyond a generous guard); reranker is called once per request
  with exactly `recall_k` candidates; a latency guard on the fusion function.
- **Maintainability** (`tests/test_retrieval_maintainability.py` or a script):
  public functions carry docstrings; no duplicated regconfig literal; module
  import graph stays torch-free; `ruff check` clean; no module in the new package
  over ~300 lines.

### T6 — Docs, spec, AGENTS, close-out, ~0.5 day

- `specs/016-session-10-hybrid-and-rerank.md` — problem, the two proposals and
  the one chosen, contracts, the deviations from the reference (`source` field,
  dual candidate counts, `rank` kept separate from `distance`), non-goals,
  verification. Note that the 502-on-reranker-failure policy is **the same as the
  reference**, not a deviation — the reference aborts too; only the message text
  and the logging are ours.
  Linked from `docs/decisions.md` (the docs-link test enforces it).
- `AGENTS.md`: endpoints (the new route), the flow table, and gotchas —
  regconfig must match the generated column; the reranker must never be imported
  by a hermetic test; lexical hits carry no cosine distance.
- `AGENTS.md` Layout: the **"RAG, two packages"** entry C2 requires —
  `app/embedding_pipeline/` is the corpus half (chunk → embed → store), and
  `app/generation/rag/retrieval/` is the query-time half (lexical + vector → RRF →
  rerank → `/v1/retrieval/search`); the split is intentional, the edge is one-way,
  and `app/generation/rag/retrieval/README.md` is the boundary note. T0 writes the
  README; T6 adds the pointer, so a reader arriving from either side finds the
  other. (Post-T6 there is no `/search`, so the corpus half's HTTP surface is
  `/embeddings/*` only.)
- `docs/architecture.md`: retire "Known limitation: no composition between
  retrieval and estimation" **only** if T0B lands; otherwise amend it to say the
  retrieval surface gained a pipeline and the composition gap remains.
- `docs/data-model.md`: `RetrievalRequest`, `RetrievalResult`, `RetrievedChunk`.
- `docs/data-flows.md`: replace "no metadata filters, no hybrid search, no
  reranking" with the accurate new statement (hybrid + reranking yes, metadata
  filters no — still forbidden); add the retrieval flow with its four
  configurations as a numbered flow.

#### T6 documentation matrix — mandatory, walked per `rules/closeout.md`

Every row is either edited or explicitly recorded as "unchanged, because". The
rule file is the checklist; this table is what session 10 puts in each entry.

| Artifact | What session 10 must say |
| --- | --- |
| `README.md` | the reranker gate command, the eval command, and one line that `rerank=true` downloads weights on first use |
| `AGENTS.md` | Layout: the two-package RAG split and the boundary README. Endpoints: `/v1/retrieval/search` in, `/search` out. The flow table gains the retrieval row. Test count updated. Four gotchas: regconfig must match the generated column; no hermetic test imports torch; lexical hits carry no cosine distance; a reranker failure is a 502, never a silent fallback |
| `docs/architecture.md` | the retrieval pipeline as built; retire "Known limitation: no composition…" **only** if T0B lands |
| `docs/data-flows.md` | the retrieval flow, four configurations, and the 502 path |
| `docs/data-model.md` | `RetrievalRequest`, `RetrievedChunk`, `RetrievalResult`; `rrf_k` ≥ 1; the `content_tsv` column |
| `docs/operations.md` | six new settings with defaults, `alembic upgrade head` for `0004`, the `verify_reranker` command, the `HF_HOME` volume, and a troubleshooting entry for the 502 |
| `docs/technologies.md` | `sentence-transformers` and its torch transitive; what breaks without it (only `rerank=true`) |
| `docs/glossary.md` | lexical branch, `tsvector`/GIN, RRF, bi-encoder vs cross-encoder, recall-then-rerank |
| `docs/decisions.md` | the spec 016 row with its status |
| `docs/README.md` | unchanged unless T6 adds a doc file; if it does, index it in the same change |
| `rules/api.md` | the new route, its bounds, and the 502 body |
| `rules/development.md` | where a new retrieval module goes (the two-package rule) |
| `rules/testing.md` | the mutation-test category, the Postgres-marked lexical tests, the no-torch-in-hermetic-tests rule |
| `.env.example` | `RRF_K`, `RERANKER_ENABLED`, `RERANKER_MODEL`, `RETRIEVAL_SEARCH_MODE`, `RETRIEVAL_RECALL_TOP_K`, `RERANK_TOP_N` with defaults and one comment line each |
| `specs/016-…` | `Implemented`, with the A–D table and the gate output pasted |
| `specs/014-…` | the "superseded for the retrieval surface" note |
| `pyproject.toml` | `sentence-transformers>=3.0`; anything else the build needs |

**Close-out evidence for T6** — the checklist is only closed with commands:

```powershell
Select-String -Path README.md,AGENTS.md,docs\*.md,rules\*.md -Pattern '/search"|POST /search'   # must show only docs/instrucctions-missing
Select-String -Path app -Recurse -Pattern 'search_router'                                        # must return nothing
git status --short
uv run pytest -q
uv run pytest -q tests/test_docs_links.py
```
- **Retire `/search`** (C4), in this order:
  1. `examples/trace_s09.py` — point `step_search` at `/v1/retrieval/search`, read
     `chunks[]` instead of `results[]`, keep `EXPECTED_FIELDS`/exit codes intact;
     re-run it against the live stack so the Session-9 artifact stays true.
  2. `scripts/query_examples.py` — same migration; regenerate
     `output_examples.txt` so the distances it prints come from the new path.
  3. `tests/test_search_endpoint.py` → `tests/test_retrieval_route.py` against the
     new route; retarget `test_embedding_schemas.py` / `test_rag_postgres.py` at
     `RetrievalRequest` where they assert route shapes.
  4. Delete `app/embedding_pipeline/search_router.py`, its `include_router` line in
     `app/main.py`, and `get_semantic_retriever` if nothing else uses it.
  5. Amend spec 014 with a one-paragraph "Superseded for the retrieval surface by
     spec 016" note; keep its 20,000-char bound documented as the surviving rule.
- **Gate:** `grep`-clean — no `"/search"` route decorator left, no caller posting
  to `/search`, and `examples/trace_s09.py` still exits 0 against the live stack.

---

## 5. Sequencing and effort

| Phase | Depends on | Effort |
| --- | --- | --- |
| T0 retrieval core | — | 1–1.5 d |
| T1 FTS migration | — | 0.25 d |
| T2 lexical + RRF + pipeline | T0, T1 | 0.75 d |
| T3 reranker (gate first) | T0, T2 (gate can run any time) | 0.5–1 d |
| T4 golden set + eval + table | T3 | 0.75 d |
| T5 tests (unit/integration/mutation/non-functional) | T2, T3 | 1 d |
| T6 docs + spec | all | 0.5 d |
| T0B generation chain | T0 | +2–3 d |
| **Total** | | **~7–9 focused days** |

Order of execution: **T3's gate first** (the brief demands it before anything
else), then T1, T0, T2, T3 wiring, T4, T5, T6.

---

## 6. Verification plan

```powershell
# 1. Hermetic suite — must stay green at every phase boundary
uv run pytest -q

# 2. Docs links and the spec index
uv run pytest -q tests/test_docs_links.py

# 3. Postgres-only surfaces
$env:TEST_DATABASE_URL = "postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest -q -m postgres

# 4. The brief's gate
docker compose up -d
docker compose exec api python -m app.generation.rag.retrieval.verify_reranker

# 5. Migration round-trip
docker compose exec api alembic upgrade head
docker compose exec api alembic downgrade -1
docker compose exec api alembic upgrade head

# 6. Corpus ingest (idempotent; 409 = already present)
docker compose exec api python scripts/query_examples.py

# 7. Four configurations
$env:ESTIMATOR_API_BASE_URL = "http://localhost:8000"
uv run python scripts/eval_retrieval_s10.py

# 8. Mutation coverage
powershell -File scripts/mutation_s10.ps1

# 9. Fusion is pure and deterministic
uv run python -c "from app.generation.rag.retrieval.fusion import reciprocal_rank_fusion as f; print(f([[1,2,3],[3,2,1]]))"
```

**Done criteria**

- [ ] The reranker gate exits 0 on this machine, and the output is pasted into the
      report.
- [ ] `chunks.content_tsv` exists with a GIN index; the DDL config and the query
      config are the same constant, proven by test.
- [ ] `search_lexical` finds a rare identifier that the vector branch misses.
- [ ] RRF fuses rankings by position, 1-based, deterministic, rejects `k <= 0`.
- [ ] All four configurations are invocable through one documented surface and
      through the CLI, and produce different result orders where expected.
- [ ] The golden set has 5 annotated queries whose ids exist in our corpus, and the
      annotations were re-read against it.
- [ ] The comparison table reports precision@5 and latency for A–D with the run
      date, machine and corpus state recorded.
- [ ] Mutation kill rate ≥ 90% on the four scoped modules, survivors listed.
- [ ] Security, performance and maintainability tests exist and run in the suite.
- [ ] The conclusions paragraph names a configuration and answers the latency
      question with the measured numbers, not an opinion.
- [ ] `/search` is gone, its two script callers use `/v1/retrieval/search`, and
      `examples/trace_s09.py` still runs end to end against the live stack.
- [ ] Spec 014 carries the "superseded for the retrieval surface" note.
- [ ] The HNSW index survives the migration round-trip.
- [ ] Spec 016 is `Implemented` and indexed in `docs/decisions.md`.
- [ ] Full `uv run pytest -q` green; no test downloads weights or reaches a provider.

---

## 7. Risks

1. **Transformers major 5 vs the reference's stack.** `uv pip install --dry-run`
   resolves `transformers==5.22.2`; the reference was written against an earlier
   major. The gate resolves it, but the `CrossEncoder` API may have moved. Pin if
   needed.
2. **The regconfig decision (C1) is a graded judgement call.** `english` matches
   the data and the course's own reference; `spanish` matches the brief's text.
   Whichever we pick, it must be documented with the evidence, not asserted.
3. **Track 0 is the real cost.** Without it, steps 2–4 have no pipeline to improve.
   With it plus T0B, the session roughly doubles.
4. **Mutation testing is new tooling.** `mutmut` on a 665-test suite will be slow;
   scope it to four modules or the run will be measured in hours.
5. **Reranker cold start.** The first call downloads weights; without an `HF_HOME`
   volume a recreate re-downloads them (fixed in T3). Because the policy is
   fail-closed, a cold start makes `rerank=true` return 502 until the model is
   loaded — so T3's gate must be run once after every `docker compose up` before
   the eval harness, or configuration C/D will record 502s as the session's
   result.
6. **The eval numbers are machine-specific.** Latency is evidence of a run, not a
   benchmark; record the machine, the corpus state and the ingest date.
7. **A copied golden set could be subtly wrong.** Ids match; the *relevance*
   judgements are the reference author's, and two of the five are debatable
   (`BUD-2024-003` appears in both Q1 and Q5's relevant sets). Re-read them.
8. **Silent index bypass.** If the migration's literal and the query's config ever
   diverge, Postgres returns rows with the GIN index unused — correct-looking, slow,
   and wrong. T5.1's parsing test is the only thing that catches it.
9. **`/v1/retrieval/search` with no auth.** Consistent with our repo, inconsistent
   with the reference. It bills an embedding call per request. Say so in the spec.
10. **Scope creep at T0.** Metadata filters, reformulation and generation are all
    one small step away and all forbidden or deferred. Keep them out.

---

## 8. Non-goals

- Query expansion / multi-query / decomposition — forbidden by the brief.
- Multi-index routing and the three-collection split — forbidden by the brief.
- Metadata filtering (sector/year/chunk-type `WHERE` clauses) — forbidden by the
  brief.
- Temporal decay, per-task hours estimation, task corpora — live-session material.
- `/v1/estimate/from-transcript`, the stage endpoints, API keys and rate limiting —
  T0B, not this session (unless you approve T0B).
- Changing `/search` — it is retired, not extended (C4).
- The chunker's text template, chunk metadata keys, the embedding model, or the
  HNSW index.
- NDCG / recall@k sweeps / reranker fine-tuning.
- `estimator-web/`, and any reference branch beyond `session_10`.

---

## 9. Open questions — answer these and the plan is executable

1. ~~**C1, regconfig**~~ — **DECIDED: `english`.** Verified 2026-11-07 that the
   reference's `data/budgets_sample.json` at `origin/session_10` is 100% English
   (0 Spanish-looking strings), as is ours; same 17 ids, same summaries.
2. ~~**C2, module path**~~ — **DECIDED: option (b).** New Session-10 modules go
   under `app/generation/rag/retrieval/` with a `README.md` marking the split;
   `app/embedding_pipeline/` keeps the corpus half and none of the 133 existing
   import lines change. `AGENTS.md` gains a "RAG, two packages" entry (T6).
3. ~~**C3, Track 0**~~ — **DECIDED: T0 + T0B.**
4. ~~**C5, deliverables**~~ — **DECIDED:** one root `session10_report.md`
   (golden set, A–D table, method notes, conclusions); the golden set itself in
   `evals/golden_retrieval.json`; the harness in `scripts/eval_retrieval_s10.py`.
5. ~~**Proposal A or B**~~ — **DECIDED: A**, and `/search` is retired in T6 (C4).
6. ~~**Mutation threshold**~~ — **DECIDED:** ≥90% killed on the four scoped
   modules, run by `scripts/mutation_s10.ps1` outside `uv run pytest -q`;
   survivors listed with a one-line justification each.
7. ~~**Golden set**~~ — **DECIDED:** adopt the reference's Q1–Q5, then re-read each
   relevance label against our corpus before committing it.
8. ~~**Reranker failure policy**~~ — **DECIDED: fail closed, 502** with
   `"Reranking service is unavailable. Please retry later."`, logged as
   `reranker_unavailable`. A 200 with un-reranked results would report a
   configuration that never ran.

Once these are answered, implementation starts at T3's gate — the brief's own
"do not proceed if it fails" step.

**All eight are now answered.** Nothing blocks implementation: the next action is
T3's environment gate (`verify_reranker`), then T1.
