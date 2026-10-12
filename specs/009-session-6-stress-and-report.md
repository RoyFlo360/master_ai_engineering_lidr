# 009 — Session 6: per-turn observation, memory drift metrics, stress runner

- **Status:** Implemented
- **Date:** 2026-10-10
- **Scope:** `app/services/estimation.py`, `app/services/llm_wrapper.py`,
  `app/config.py`, `app/dependencies.py`, `app/routers/sessions.py`,
  `evals/stress/{scenarios,metrics,run}.py`, `evals/stress/fixtures/build_pdfs.py`,
  `evals/stress/REPORT.md`, `tests/`, `.env.example`, `AGENTS.md`, `README.md`,
  `MISSING.md`
- **Supersedes / Superseded by:** —

## Problem

The Session-6 brief asks for an empirical map of *where the Cache-Augmented
Generation pipeline breaks*: at which turn does memory drift, how latency and cost
grow with context and attachment size, and how long a conversation can run before
it stops being viable. To answer that we need three things the codebase did not
have:

1. **A single, parseable per-turn telemetry event.** The pipeline emitted a dozen
   separate log lines (`cache_hit`, `llm_call_completed`, `history_compressed`, …)
   per turn. Building a CSV meant reconciling timestamps across lines.
2. **Synthetic multi-turn conversations with tracked facts.** No script produced
   the "growing / pivot / contradiction" profiles, nor declared *which* fact each
   turn must be remembered by.
3. **Three deterministic metrics + a runner + a report.** Nothing measured
   latency/cost budgets or memory drift, orchestrated the runs, or produced the
   deliverable.

Additionally, the auxiliary LLM calls (metadata extractor, summariser, critic,
anchor classifier) default to an OpenAI model. On this machine that provider's
free tier is gated, so those calls silently degraded (empty summary, critic
`accept` at confidence 0) even though the main estimation kept working — which
would have made a "recall vs N" curve meaningless.

## Goals

- Every `POST /sessions/{id}/estimate` emits one `turn_observed` event with the 13
  required fields, and the same object is on the response.
- Three multi-turn profiles with fact trackers, runnable as N ∈ {1,3,6,10,20}.
- Synthetic PDFs at 0/5/20/50/100 KB, generated deterministically, not committed.
- `LatencyBudgetMetric`, `CostBudgetMetric`, `MemoryDriftMetric` + unit tests.
- A runner that writes a ≥50-row CSV, and a populated `REPORT.md`.
- Auxiliary calls tolerate a dead provider via an optional fallback model.

## Non-goals

- No embeddings / LLM-as-judge metric (determinism over sophistication).
- No chart rendering — the report's curves are tables.
- No change to the estimation prompt or the ACB loop; ACB does not emit
  `observation` (the brief only requires it for `/estimate`).
- No RAG implementation — the report only argues for it from the data.

## Design

**Observation (Block 1).** `TurnObservation` (`app/schemas/new_estimation_schema.py`)
carries the 13 fields. `EstimationService.estimate_conversational` builds it right
before `return`, logs `turn_observed`, and returns it as
`StructuredEstimationResponse.observation`. Transport choice: **embedded in the POST
response**, not scraped from stdout — single-pass, no timestamp reconciliation, and
the stress runner reads it directly from the JSON.

**Snapshot memory (needed for drift).** `GET /sessions/{id}` previously exposed only
`summary_chars` / `anchors_count` — the *sizes* of the memory, not the text, so a
drift metric could not search it. `SessionInfoResponse` now also returns `summary`
(text) and `anchor_texts` (list), making the debug endpoint show the memory it claims
to expose.

**Drift metric (Block 4).** `MemoryDriftMetric(fact, where=["summary","anchors",
"metadata"])` searches those slots case-insensitively. `where` follows the brief; the
old single-field selector is kept as a keyword alias. It lives in
`evals/stress/metrics.py` (not `evals/metrics.py`) because it reads a
`TurnObservation` / session snapshot, not an `(GoldenCase, EstimationResult)` pair —
different input types justify a sibling module. `MetricResult` is reused verbatim.

**Profiles and N (Block 2).** `Scenario.truncated(n)` returns a shorter view,
bypassing the "≥20 turns" guard that protects the canonical masters. Because turn
*k*'s telemetry depends only on turns 1..k, one walk to `max(N)` yields every N data
point: the row for `turn_index=k` *is* the N=k observation. `--turns` sets the report
set; `n_in_set` marks the rows.

**Attachments (Block 3).** `fixtures/build_pdfs.py` builds deterministic PDFs with
`fpdf2`; `0 KB` is the absence of a file. `MAX_ATTACHMENT_CHARS` was raised to
`60_000` so the 50 KB point sits under the cap and the 100 KB point truncates — the
brief's intent.

**Runner (Block 5).** `evals/stress/run.py` orchestrates `scenarios × sizes × repeats
× turns`, writes a CSV (13 telemetry fields + `wall_clock_ms` + metric verdicts +
per-turn recall), prints a P50/P95 summary, and `--append` lets two cheaper passes
build one CSV. `--cache-probe` measures the real exact/semantic cache on
`/api/v1/new-estimate` (the session endpoint never caches, so its hit rate is
structurally 0).

**Aux fallback (D15).** `LLMWrapper.complete_structured_chat` retries once on
`aux_fallback_model` when a call that passed `model_override` (i.e. an auxiliary
call) fails. The main estimation call passes no override and is never re-routed.

## Contract

```
POST /sessions/{id}/estimate → 200 {result, prompt_version, cache_hit, observation}
  observation = {turn_index, session_id, enriched_transcript_chars,
                 attachments_total_chars, messages_in_window, anchors_count,
                 summary_chars, tokens_in, tokens_out, cost_usd, latency_ms,
                 cache_hit_kind, last_resolved_tier}
  and a structlog event: log.info("turn_observed", <the 13 fields>)

GET /sessions/{id} → 200 {…, summary, anchor_texts}      # + existing fields

python -m evals.stress.run --http URL --scenarios … --attachment-sizes …
    --repeats N --turns 1,3,6,10,20 [--append] [--cache-probe] --output CSV
```

## Verification

- `uv run pytest -q` → **260 passed** (was 249). New/updated:
  `tests/test_stress_metrics.py` (the `where` list + legacy alias),
  `tests/test_stress_runner.py` (new signature + `Scenario.truncated`),
  `tests/test_llm_wrapper.py` (aux fallback: fires, doesn't fire for the main call,
  disabled re-raises, equal-model no-retry).
- `python -m evals.stress.fixtures.build_pdfs` → 5/20/50/100 KB PDFs; the 100 KB
  extract (~102 573 chars) exceeds the 60 000 cap and truncates.
- `python -m evals.stress.run …` → `results.csv` with ≥50 rows; report filled from it.
- Live: `GET /sessions/{id}` returns `summary` + `anchor_texts`.

Not verified: no real-provider run of the eval **golden set** in this spec; the
stress numbers depend on the model actually used (see `REPORT.md`).

## Follow-ups

- `EstimationService.estimate` is still not wired to a route (spec 007).
- The report's attachment "recall within the summary" is a stated gap — the three
  brief metrics do not include a content-recall metric for attachments.
- Two pricing tables remain un-unified (spec 001).
