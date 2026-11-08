# 017 — Session 10: grounded estimation from a transcript

- **Status:** Implemented
- **Date:** 2026-11-08
- **Scope:** `app/generation/rag/` (new: `schemas.py`, `errors.py`,
  `observability.py`, `query_reformulator.py`, `context_assembler.py`,
  `estimator.py`, `validation.py`, `idempotency.py`, `orchestrator.py`,
  `estimate_router.py`), `app/prompts/rag_reformulation/v1/`,
  `app/prompts/rag_grounded_estimation/v1/`, `app/config.py`, `app/main.py`,
  `docs/**`, `AGENTS.md`
- **Supersedes / Superseded by:** Supersedes the non-goal in spec
  [016](016-session-10-hybrid-and-rerank.md) reading "grounded generation … did
  not ship". Nothing supersedes this spec.

## Problem

Retrieval had no consumer. `POST /v1/retrieval/search` returns ranked chunks with
scores and provenance, and every caller stopped there: the retrieval eval, the
query-examples script and the Session-9 trace display evidence and never estimate
from it. An estimation request and the corpus lived in the same process and never
met.

The Session-9 diagnosis in `arquitectura-actual.md` measured the cost: a real
meeting transcript came back as five ranked budget components, with no aggregation
to budget level, no relevance floor, no grounded answer and no way to tell a good
match from a poor one. Session 10 shipped the retrieval half (spec 016) and
deferred the generation chain;
`../docs/instrucctions-missing/MISSING-session10.md` §1 records the same gap and is
the source of the chain's shape.

## Goals

- One entry point: transcript in, estimate with per-claim evidence out.
- Reformulation from Spanish transcript to the English string the corpus can match,
  plus an honest statement of how complete the brief was.
- A pure assembler emitting addressable `<source>` blocks under a token budget, able
  to say exactly which ids it emitted.
- Citations validated against the ids that reached the prompt, with one retry; an
  explicit ungrounded answer when nothing clears a relevance floor.
- Per-stage routes for hand inspection, and idempotent replay for the paid route.

## Non-goals

- **Metadata filters, query expansion, multi-index routing.** Forbidden by the
  Session-10 brief. `EstimationQuery` carries `sector`, `technologies` and
  `project_year`; the chain does not filter on them.
- **A Redis-backed idempotency store** (a follow-up), **streaming** (one complete
  `GroundedEstimate` per request) and **auth** (no route here declares a scheme).
- **Changing retrieval.** The chain calls `retrieval.pipeline.retrieve`; it does
  not re-implement it, and `/v1/retrieval/search` is untouched.

## Design

### The chain, in order

`orchestrator.estimate_from_transcript` is the composition point, and the order is
explicit rather than hidden behind a framework:

```text
idempotency lookup -> reformulate -> retrieve -> assemble -> generate
                   -> check_citations (one corrective retry) -> store -> response
```

An idempotent hit short-circuits before reformulation. Empty retrieval
short-circuits before assembly and generation: there is nothing to ground an
estimate in, so the chain answers `grounded=False` instead of asking the model to
recall. The response is stored after success only, so a failed chain is retried,
never replayed.

### Why it is split into modules

Each module owns one decision, and the ones a test can pin without a provider are
free of I/O:

- `query_reformulator` — transcript → `EstimationQuery` including the non-fatal
  fallback; tests inject a fake wrapper instead of a provider.
- `context_assembler` — chunks → `<source>` text **and** the allowed id set,
  **pure**: no I/O, no async, no LLM, so budget arithmetic is exactly testable.
- `estimator` — the one grounded generation turn and its prompt.
- `validation` — citations vs. allowed ids; pure, while the *policy* on failure
  lives in the orchestrator because it costs a provider call.
- `idempotency` — key derivation and the bounded store, a process singleton.
- `observability` / `errors` — one stage timer; the domain errors **and their HTTP
  status**, so the mapping cannot drift per route.
- `orchestrator` / `estimate_router` — the order (readable in one function) and
  transport only.

### Why the assembler is pure, and why the check uses `sources_of`

A chunk dropped for budget cannot be cited. Comparing against the retrieved list
instead of `sources_of(context)` would let an estimate cite evidence the model never
saw and still pass — the failure the check exists to prevent — so `assemble` and
`sources_of` share one inclusion decision and cannot disagree.

Blocks are XML-like (`<source id=… budget=… sector=… year=…>`) with escaped
attributes and verbatim content, because the content is the evidence to be quoted. A
chunk containing a literal `</source>` could forge a block, but a forged id is not in
`sources_of`, so validation rejects it — fail-closed, not silent. Truncation is
**whole-chunk** (a chunk cut mid-sentence reads like a fact but cannot be cited
correctly), the first chunk is always kept (an empty context turns a grounded
estimate into an ungrounded one), and rank order is never re-sorted — sorting by
distance would undo the fusion and the reranker.

### Why the distance floor lives in the chain

`ChunkStore.search` without a floor always returns `k` chunks, so an unrelated brief
came back `grounded=True` on five irrelevant budgets. The floor belongs to the chain,
not `/v1/retrieval/search`, because the callers want opposite things: the route is an
inspection primitive (show the nearest chunks, even poor ones) while the chain must
report "the corpus has no analogue". Calibration on this corpus, recorded in
`app/config.py` and measured 2026-11-08: relevant queries 0.43–0.60, an unrelated
topic ("satellite imagery of coral reefs") 0.71–0.72, hence
`RETRIEVAL_DISTANCE_THRESHOLD = 0.65`. Before the floor an unrelated brief returned
`grounded=True` with five irrelevant citations; after it, `grounded=False` with zero.
Re-tune if the embedding model or the chunking template changes.

### Why the fallback is non-fatal, and why idempotency is in-process

Reformulation is advisory: searching the transcript itself is worse than searching a
distilled query, but much better than failing the request or retrieving nothing, so
every LLM failure — missing template, unbuildable wrapper, timeout, wrong return
type, empty `search_query` — degrades to a truncated transcript with
`source="fallback"` and `confidence` capped at `FALLBACK_CONFIDENCE_CAP = 0.3`. A
summary without a query is capped the same way: a transcript-derived query must not
claim to be a distilled one. Only a transcript empty after cleaning is fatal
(`ReformulationError`).

The store only has to stop a retry from paying twice, so it is a thread-safe
256-entry FIFO `OrderedDict` keyed by `sha256(idempotency_key + "\x00" + transcript)`
— hashing the transcript with the key stops the same key with different text from
replaying an earlier estimate. Nothing survives a restart and each uvicorn worker has
its own store; Redis is the production answer.

## Contract

All five routes are under `/v1/estimate`, declare no auth, and declare
`response_model`. Bodies forbid unknown fields.

```text
POST /v1/estimate/from-transcript
{"transcript": "…",          # 100..50,000
 "idempotency_key": "…",     # optional, <=128
 "top_k": 5,                 # 1..50
 "rerank": true|false|null,  # null -> RERANKER_ENABLED
 "search_mode": "vector"|"hybrid"|null}   # null -> RETRIEVAL_SEARCH_MODE
→ 200 {"estimate": {summary, modules[{name, tasks[{name, estimated_hours}],
                                     estimated_hours}], total_hours,
                    citations[{chunk_id, budget_id, claim}],
                    confidence_pct, grounded},
       "query": {summary, search_query, sector, technologies, project_year,
                 confidence, source},
       "chunks": [...], "retrieved": 5, "cached": false,
       "validation_retries": 0, "pipeline_ms": 4210}
```

`validation_retries` is 0 or 1 — one retry, never a loop. A replay returns
`cached=true` with the **stored** `pipeline_ms`: how long the estimate took to
produce, not how fast the cache answered.

| Stage route | Body | Returns |
| --- | --- | --- |
| `/stages/reformulate` | `{transcript}` | `{stage, latency_ms, query}` |
| `/stages/retrieve` | exactly one of `{query 1..20,000}` or `{estimation_query}`, plus `top_k 1..50`, `search_mode`, `rerank`, `rrf_k 1..1,000`, `distance_threshold 0..2` | `{stage, latency_ms, result, chunks}` |
| `/stages/assemble` | `{chunks[≥1], max_tokens}` (default 3,000) | `{stage, latency_ms, context, sources}` |
| `/stages/generate` | `{query, context ≤200,000, source_ids[], model_override}` | `{stage, latency_ms, estimate}`; does **not** validate citations |

The stage routes are stateless and reuse the chain's functions — a debugging surface,
not a second API. Each domain error carries its own status and public message:

| Status | Body | Raised when |
| --- | --- | --- |
| 422 | FastAPI validation detail | a body bound fails, or an unknown field is sent |
| 422 | `"No context available to build an estimate."` | `ContextAssemblyError` |
| 502 | `"Could not interpret the transcript."` | `ReformulationError` — a blank transcript |
| 502 | `"The estimation model is unavailable. Please retry later."` | `GenerationError` |
| 502 | `"The estimate could not be grounded in the retrieved evidence."` | `CitationValidationError`, after the one retry |
| 500 | `"Failed to estimate from the transcript."` | anything else, no exception text. `RetrievalUnavailable` is a `RuntimeError`, not a `ChainError`, so an unwired embedder or store also lands here |

## Verification

Recorded 2026-11-08; every number is copied from a run.

- **Default suite** — `uv run pytest -q` → **813 passed, 24 skipped, 3 warnings**.
  The 24 skips are the `postgres`-marked modules.
- **Real database** — with `TEST_DATABASE_URL` set to the compose Postgres,
  `uv run pytest -q -m postgres` → **24 passed, 813 deselected**.
- **The floor, before and after** — the comment in `orchestrator._ground` records
  the failure it fixes: without a floor the vector branch always returns `top_k`
  chunks, so an unrelated brief came back `grounded=True` on five irrelevant
  budgets. With 0.65 the same brief returns `grounded=False`, `retrieved=0`, zero
  citations. The calibration behind the value is in `app/config.py`.
- **An empty retrieval is a 200** —
  `tests/test_estimate_transcript_route.py::test_an_empty_retrieval_is_a_200_with_grounded_false`.
- **Hermetic coverage** — `tests/test_estimate_transcript_route.py` (grounded 200
  shape, empty retrieval, 422 before any stage, 502 without leaked detail, replay, a
  different key not replaying, each stage route's shape, `extra="forbid"`, routes in
  `/openapi.json`), `tests/test_reformulator.py` (fallbacks, confidence cap,
  summary-without-query repair, empty transcript), `tests/test_context_assembler.py`
  (block format, whole-chunk truncation, first chunk kept, rank order, `sources_of`
  matching `assemble`), `tests/test_grounded_estimate.py` (empty context never calls
  the model, `grounded` reflects context, `GenerationError`, `check_citations`).

Not verified: **the two live provider runs were not re-executed by this documentation
pass** (they need a live provider and an ingested corpus, and cost money; those
numbers are the recorded implementation observations plus the `app/config.py`
calibration — the suite figures were re-run); **estimate quality**, because no golden
set of transcripts with expected hours exists, so the evidence is structural;
**provider behaviour under load** (two LLM calls in the normal path, three with the
retry, and no latency budget measured beyond `pipeline_ms`).

## Follow-ups

- **Redis-backed idempotency** — survives restarts and is shared across workers;
  needs a TTL policy and a degradation path for an unreachable Redis.
- **Metadata filters** — `sector`/`technologies`/`project_year` are extracted and
  unused; filtering is a retrieval change the Session-10 brief forbids.
- **Budget-level aggregation** — retrieval returns components, and
  `documents.metadata` still does not persist `total_estimated_hours`, so parent-hour
  rollup is not possible from the store.
- **A judged transcript set and streaming** — without expected estimates there is no
  way to measure whether the `grounded=False` cases are the right ones; the stage
  split makes streaming structurally easy.
