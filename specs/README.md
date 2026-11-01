# Specs — spec-driven development

Every change that is bigger than a bug fix gets a spec here **before** the code.
The spec is the contract: it says what problem is being solved, what the API
looks like, and how the result was verified. Code that contradicts an accepted
spec is a bug in one of the two — decide which, then fix it.

> **Renumbering, 2026-09-27.** 001–003 originally held the typed-flow, usage and
> totals specs. Backfilling the surface that predates this folder moved those three to
> **004–006**, so that the numbers follow the order the system was actually built in.
> An older note saying "spec 001/002/003" about the typed flow, usage reporting or the
> totals means 004/005/006 today. No number was reused.

## Workflow

1. Copy `TEMPLATE.md` to `NNN-short-kebab-title.md`, the next free number.
2. Fill it in and set **Status: Draft**. Drafts are cheap; argue here, not in review.
3. Move it to **Accepted** when the approach is settled, then implement.
4. After implementing, fill in **Verification** with the real evidence (test names,
   commands you actually ran, observed output) and set **Status: Implemented**.
5. If a later spec overrides this one, set **Superseded by NNN** rather than deleting
   it — the reason a decision changed is worth as much as the decision.

Statuses: `Draft` · `Accepted` · `Implemented` · `Superseded by NNN`.

## Rules

- One spec per change, numbered in creation order. Numbers are never reused.
- Write the API contract as it will actually be called: fields, types, status codes.
- "Verification" means evidence, not intention — name the tests and paste the output
  that proves it. If something was not verified, say so in its own line. A spec that
  claims more than was checked is worse than no spec.
- Record the **non-goals**. Half of the value of a spec is what it refuses to do.
- Ground truth lives in the code. If a spec and the code disagree, check both before
  trusting either.

## Index

- [001 — Transcription estimation endpoint](001-transcription-estimation-endpoint.md)
  — Implemented · `POST /api/v1/estimate`, the prompt with reference examples, and the
  truncation guard that stops an empty estimation coming back as a 200
- [002 — Streaming estimation endpoint](002-streaming-estimation-endpoint.md)
  — Implemented · SSE frames, the wire-ready event vocabulary, and why the blocking
  generator runs in a threadpool
- [003 — Exact-match Redis cache (CAG)](003-exact-match-redis-cache.md)
  — Implemented · key derivation from the rendered prompt, the versioned prefix, and
  degrading to uncached when Redis is unreachable
- [004 — Prompt v1 estimation endpoint](004-prompt-v1-estimation-endpoint.md)
  — Implemented · typed request, versioned Jinja prompts, and the `response_model` bug
  that turned every success into a 500
- [005 — Usage and cost reporting, including cache hits](005-usage-cost-and-cache-hit-reporting.md)
  — Implemented · why a replay reports the cached tokens at zero cost, and why the two
  flows report a replay differently on purpose
- [006 — Running usage totals](006-running-usage-totals.md)
  — Implemented · the process-local ledger and `GET /api/v1/usage`, with the
  restart/reload limit recorded as a known one
- [007 — Conversational sessions, attachments and the structured response split](007-conversational-sessions.md)
  — Implemented · the `/sessions` endpoints, why `EstimationResponse` earned a
  sibling instead of a required `result` field, and the semantic-cache degradation
  that lets vanilla Redis keep serving requests
- [008 — Session 6: hybrid memory, audience tiers, Actor-Critic-Boss, evals](008-session-6-memory-tiers-and-acb.md)
  — Implemented · anchor/ summary compression, the tier resolver and prompt v3, the
  Critic+Boss loop and its audit trail, per-turn telemetry, and the `evals/` and
  `evals/stress/` harnesses
- [009 — Session 6: per-turn observation, memory drift metrics, stress runner](009-session-6-stress-and-report.md)
  — Implemented · the 13-field `turn_observed` event, `MemoryDriftMetric`, the
  `N ∈ {1,3,6,10,20}` scenario builder, and the filled stress report
- [010 — Session 7: embedding pipeline](010-session-7-embedding-pipeline.md)
  — Implemented · structural chunking with a contextual header, batched
  `text-embedding-3-small` calls, `POST /embeddings/ingest`, and the three-pair
  cosine sanity check
- [011 — Session 7: data ingestion](011-session-7-data-ingestion.md)
  — Implemented · the versioned catalog, JSON/TXT parsers, cleaning + Pandera
  validation and pseudonymization **inside** the run, job tracking, and the
  architecture-decision CLI
- [012 — Session 8: pgvector persistence and semantic search](012-session-8-pgvector-and-search.md)
  — Implemented · `documents`/`chunks` on a separate `RagBase`, a dialect-guarded
  migration, one-transaction ingest with a unique `source_path`, and k-NN
  search by cosine distance in SQL — with no vector index, on purpose
- [013 — Session 8 extras: runtime model configuration and the chunking lab](013-session-8-runtime-config-and-chunking-lab.md)
  — Implemented (chunking lab) · eight chunkers behind one interface, the comparison endpoint and CLI, and a real `COMPARISON_REPORT.md`. The runtime model-configuration half is deferred.
- [014 — Session 9: architectural diagnostic and the search query bound](014-session-9-prework-trace.md)
  — Implemented · `arquitectura-actual.md`, the three probe transcripts and `examples/trace_s09.py`, plus one code change: `MAX_QUERY_CHARS` 2,000 → 20,000. No retrieval behaviour was added.

## How these were written

**001–003 are backfilled**: they describe code that already existed, written at the end
of the session that built 004–006 rather than before it. They read as a record, and
their "Verification" sections say plainly where evidence is missing — spec 003 has no
test coverage at all, which is now written down instead of being assumed.

**004–006 were also written after their code**, for the same reason: the folder was
created at the end of that session.

**007 was written after the Session-5 code**, documenting the fix that made it run
(schema split, router mount, missing settings and prompts). Everything from **008 on
is written first**, which is the point of the folder.

**012 is Implemented** with its evidence recorded. **013 is partly implemented**:
the chunking lab landed and produced `COMPARISON_REPORT.md`; the runtime
model-configuration half was deferred, and the spec says so in its own words.
