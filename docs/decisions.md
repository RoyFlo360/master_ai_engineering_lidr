# Decisions

An index of the numbered specs in `specs/`. Each spec is the contract for a change:
problem, decision, API, verification. Open the file for the reasoning; this page is
for finding *that* a decision exists.

| # | Spec | Status | One line |
| --- | --- | --- | --- |
| 001 | [Transcription estimation endpoint](../specs/001-transcription-estimation-endpoint.md) | Implemented | `POST /api/v1/estimate` from a raw transcription, with a prompt built from reference examples and a truncation guard so an empty answer cannot return 200. |
| 002 | [Streaming estimation endpoint](../specs/002-streaming-estimation-endpoint.md) | Implemented | SSE streaming with a fixed frame vocabulary (`token`, `usage`, `done`, `error`) and the blocking generator driven from a threadpool. |
| 003 | [Exact-match Redis cache (CAG)](../specs/003-exact-match-redis-cache.md) | Implemented | Cache keys derived from the rendered prompt plus generation knobs, a versioned prefix, and degradation to uncached when Redis is unreachable. |
| 004 | [Prompt v1 estimation endpoint](../specs/004-prompt-v1-estimation-endpoint.md) | Implemented | The typed `/new-estimate` flow and versioned Jinja prompts, including the `response_model` mistake that turned every success into a 500. |
| 005 | [Usage and cost reporting, including cache hits](../specs/005-usage-cost-and-cache-hit-reporting.md) | Implemented | Why a replay reports cached tokens at zero cost on `/new-estimate`, and why streaming reports zeros instead. |
| 006 | [Running usage totals](../specs/006-running-usage-totals.md) | Implemented | The process-local `UsageLedger` and `GET /api/v1/usage`, with the restart/reload limitation recorded as known. |
| 007 | [Conversational sessions, attachments and the structured response split](../specs/007-conversational-sessions.md) | Implemented | The `/sessions` endpoints, local pdf/docx extraction, the separate `StructuredEstimationResponse`, and semantic-cache degradation. |
| 008 | [Session 6: hybrid memory, audience tiers, Actor-Critic-Boss, evals](../specs/008-session-6-memory-tiers-and-acb.md) | Implemented | Anchors plus rolling summary, the tier resolver and prompt v3, the Critic+Boss loop with its audit trail, per-turn telemetry, and the eval harnesses. |
| 009 | [Session 6: per-turn observation, memory drift metrics, stress runner](../specs/009-session-6-stress-and-report.md) | Implemented | One structured per-turn telemetry event, drift metrics, the stress CLI and its report. |
| 010 | [Session 7: embedding pipeline](../specs/010-session-7-embedding-pipeline.md) | Implemented | Structural chunking of budget JSON, batched OpenAI embeddings, a typed ingest response, and a standard-library cosine script. Persisted nothing — superseded by 012. |
| 011 | [Session 7: data ingestion](../specs/011-session-7-data-ingestion.md) | Implemented | Catalog-driven ingestion with clean/validate, pseudonymization, job records and two HTTP endpoints. |
| 012 | [Session 8: pgvector persistence and semantic search](../specs/012-session-8-pgvector-and-search.md) | Implemented | `documents`/`chunks` on their own declarative base, a dialect-guarded migration, one-transaction ingest with a unique `source_path`, and k-NN search by cosine distance. No vector index, by design. |
| 013 | [Session 8 extras: chunking lab (and runtime model configuration, deferred)](../specs/013-session-8-runtime-config-and-chunking-lab.md) | Implemented (chunking lab) | Eight chunkers behind one `Chunker` ABC, compared by `POST /embeddings/compare` and `scripts/compare_chunkers.py`, with the results in `COMPARISON_REPORT.md`. Runtime model overrides (`GET/PUT /api/v1/config/models`) were deferred. |

`specs/TEMPLATE.md` is the starting point for a new spec; `specs/README.md` explains
the workflow and status vocabulary.

## Notes on the index

- The numbering was reordered once: specs that were briefly 001–003 became 004–006 so
  the numbers follow the order the system was built in. No number was reused, and a
  reference to "001" in an older note about the typed flow means spec 004 today.
- One artefact of that reorder survives: the H1 inside
  `specs/004-prompt-v1-estimation-endpoint.md` still reads "001 — Prompt v1 estimation
  endpoint". The file name and this index are authoritative; its Status and Scope
  lines are correct.
- Specs 001–003 and 004–006 were written after their code, as a record. From 008 on,
  the spec precedes the implementation.
- 010 and 011 were written before their code and are now Implemented; their
  Verification sections carry the real test and command output.
- 012 is **Implemented** (pgvector persistence and semantic search) with real
  evidence recorded. 013 is **partly Implemented**: the eight-strategy chunking
  lab ships with its report; the runtime model-configuration half was deferred by
  decision, and the spec records why.
