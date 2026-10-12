# Session 6 — Implementation Plan (`Instructions.txt`)

> **Status:** DRAFT — awaiting review. No code has been modified for this plan.
> **Source of truth:** `Instructions.txt` (the Session-6 brief).
> **Scope note:** much of this brief was already built in an earlier pass this session
> (ported from the reference `ai-engineering/estimator`). This plan therefore first
> **audits what exists**, then lists the **real deltas** that still separate our repo
> from the brief. Only the deltas need code.

---

## 0. TL;DR

| Block | Requirement | Status today | Work left |
|---|---|---|---|
| 1 | `turn_observed` with 13 fields, emitted by `POST /sessions/{id}/estimate` | ✅ Done | none (verify only) |
| 2 | 3 multi-turn profiles, **N ∈ {1,3,6,10,20}** | ⚠️ Partial (fixed 20 turns, no N) | **D2, D3** |
| 3 | PDF fixtures 0/5/20/50/100 KB, `MAX_ATTACHMENT_CHARS=60,000` | ⚠️ Partial (builds 5–100 KB; cap is 20 000) | **D4** |
| 4 | 3 metrics + unit tests | ⚠️ Partial (`MemoryDriftMetric` signature differs) | **D1, D10** |
| 5 | Runner + CSV ≥ 50 rows + filled `REPORT.md` | ⚠️ Partial (runner & CSV exist; REPORT is a placeholder) | **D5, D6, D7, D8, D9** |

Rough effort: **~2–4 h of code**, then **one real stress run** whose wall-clock and
dollar cost are the dominant risk (see §5). The run needs an explicit go-ahead.

---

## 1. Audit — what the brief asks vs. what we have

Evidence is `file:line` as of now.

### Block 1 — unified per-turn observation  ✅ DONE
- Field list lives in `app/schemas/new_estimation_schema.py` → `TurnObservation`
  (13 fields: `turn_index, session_id, enriched_transcript_chars,
  attachments_total_chars, messages_in_window, anchors_count, summary_chars,
  tokens_in, tokens_out, cost_usd, latency_ms, cache_hit_kind, last_resolved_tier`).
- Emitted in `app/services/estimation.py::estimate_conversational` at
  `observation = TurnObservation(...)` (l.285) and
  `log.info("turn_observed", **observation.model_dump())` (l.300), immediately before
  `return`.
- Chosen transport: the observation is **embedded in the POST response**
  (`EstimationResponse.observation`), not scraped from stdout. The brief offers
  "parse logs **or** expose it via `GET /sessions/{id}`"; we chose a third, simpler
  variant. **Justify this in `REPORT.md` (§4 of the report).**
- Note: the ACB endpoint (`estimate_with_acb`) does **not** emit `turn_observed`. The
  brief's "done" criterion only covers `POST /sessions/{id}/estimate`, so this is
  in-scope-adjacent, not a gap. **Decision D11.**

### Block 2 — multi-turn profiles  ⚠️ PARTIAL
- `evals/stress/scenarios.py` defines the three profiles (`growing`, `pivot`,
  `contradiction`), each with a 20-turn script and a `fact_introduced` /
  `fact_field` tracker per turn — exactly the "fact-tracker" the brief describes.
- **Gap:** the profiles are fixed at 20 turns. `Scenario.__post_init__` (l.57)
  *hard-raises* when `len(turns) < 20`. There is **no way to ask for N ∈ {1,3,6,10,20}**.

### Block 3 — attachment corpus  ⚠️ PARTIAL
- `evals/stress/fixtures/build_pdfs.py` builds `attach_{5,20,50,100}kb.pdf`
  deterministically with `fpdf2` (already a declared dependency). Size `0` = no
  attachment, handled by the runner. ✅
- **Gap:** the brief specifies `MAX_ATTACHMENT_CHARS = 60,000`; ours is `20_000`
  (`app/config.py:44`). At 20 000 the 20/50/100 KB points all truncate identically, so
  the brief's "100 KB measures *near the limit* truncation" data point collapses.

### Block 4 — three metrics  ⚠️ PARTIAL
- `evals/stress/metrics.py` defines all three, reusing `MetricResult` from
  `evals/metrics.py`. Location choice (`evals/stress/` because they read
  `TurnObservation` + the session snapshot, not `EstimationResult`) — **justify in
  `REPORT.md`**.
- `LatencyBudgetMetric(budget_ms).evaluate(observation)` ✅ matches the brief.
- `CostBudgetMetric(budget_usd).evaluate(observation)` ✅ matches.
- **Gap:** `MemoryDriftMetric(fact, fact_field="any")` vs the brief's
  `MemoryDriftMetric(fact, where=["summary","anchors","metadata"])`. Ours takes a single
  field name; the brief takes a **list of locations**.
- Unit tests exist (`tests/test_stress_metrics.py`) but assert the current signature.

### Block 5 — runner + report  ⚠️ PARTIAL
- `evals/stress/run.py` exists with `--http`, `--scenarios`, `--attachment-sizes`,
  `--repeats`, `--latency-budget-ms`, `--cost-budget-usd`, `--output`; writes a
  22-column CSV (all 13 `turn_observed` fields + `wall_clock_ms` + 3 metric verdicts +
  `tracked_fact` + `error`); prints a P50/P95/cost/drift summary. ✅
- **Gaps:**
  - No N iteration (Block 2).
  - Only turn-1's declared fact is tracked (`tracked_fact = scenario.turns[0]...`,
    l.196); the brief wants **each turn's** fact tracked.
  - No cache-hit-rate columns (brief's summary table wants exact **and** semantic).
  - `evals/stress/REPORT.md` is a **placeholder skeleton** (34 `_xxx_` markers) — not a
    deliverable yet.

### "Done" criteria — current verdict
1. `turn_observed` per `/sessions/{id}/estimate` with 13 fields — ✅ (verify).
2. Runner end-to-end leaving **≥ 50 rows** — ⚠️ runner needs D3/D5; then a **real run**.
3. Metrics + passing unit tests — ⚠️ after D1/D10.
4. `REPORT.md` with table + 3 curves + 2 quantitative paragraphs — ❌ not yet (D8).

---

## 2. Deltas to implement (the actual work)

- **D1 — Align `MemoryDriftMetric` with the brief.**
  Change the constructor to `MemoryDriftMetric(fact: str, where: list[str] | None = None)`
  with `where` defaulting to `["summary", "anchors", "metadata"]`; keep `fact_field`
  as an accepted alias for backward compatibility (the runner and tests already use it)
  or migrate them. `evaluate(session_snapshot)` searches only the named locations,
  case-insensitive.
  *Files:* `evals/stress/metrics.py`, callers in `evals/stress/run.py`, tests.

- **D2 — Make scenarios N-parameterizable.**
  Keep the 20-turn masters. Add a way to obtain a length-N view:
  e.g. `Scenario.truncated(n) -> Scenario` (or a `scenario_turns(name, n)` helper) and
  relax `__post_init__` so it enforces the ≥20 invariant only on the *canonical*
  profiles, not on truncated copies. The brief's N set: `{1, 3, 6, 10, 20}`.
  *Files:* `evals/stress/scenarios.py`.

- **D3 — Runner iterates over N.**
  Add `--turns 1,3,6,10,20` (default = the brief's set) and a loop level above the
  scenario loop; add a `turns_planned` column to the CSV so the truncated runs are
  distinguishable. Keep `--scenarios`, `--attachment-sizes`, `--repeats`, `--output`.
  *Files:* `evals/stress/run.py`.

- **D4 — `MAX_ATTACHMENT_CHARS` → 60 000.**
  Set the default to `60_000` in `app/config.py`, update `.env.example`, and note it in
  `AGENTS.md`/`README.md`. This makes the brief's 50 KB (near-limit) vs 100 KB (truncated)
  distinction observable.
  *Files:* `app/config.py`, `.env.example`, docs. **Decision D12.**

- **D5 — Per-turn fact tracking in the runner.**
  Instead of tracking only turn-1's fact, evaluate the declared fact of *every* turn
  k against the snapshot of each later turn N > k, and aggregate a recall rate.
  Keep the turn-1 `project_name` row as the canonical headline (the brief highlights it)
  but also emit per-turn recall so the "recall vs N" curve is real.
  *Files:* `evals/stress/run.py`.

- **D6 — Cache-hit-rate columns.**
  The conversational path bypasses both caches, so `cache_hit_kind` is always `"none"`
  and both hit rates are 0%. Two options:
  - **(a) default:** add `exact_cache_hit` / `semantic_cache_hit` columns that will read
    0, and explain in the report that the brief's cache columns are trivially zero for
    the session endpoint *by design*.
  - **(b) optional:** have the runner also fire the *cached* `/api/v1/new-estimate` once
    per (scenario, size) so the exact/semantic hit rate is measured for real.
  Recommend **(a)** + a short justification; **(b)** only if the reviewer wants a live
  cache number. **Decision D13.**

- **D7 — Run the stress test for real** (see §5 for cost). Produces
  `evals/stress/results.csv` (≥ 50 rows). Requires `build_pdfs` first and the aux
  models working (see §5 prerequisite).

- **D8 — Fill `evals/stress/REPORT.md`.**
  Replace every placeholder with real numbers from `results.csv`: the summary table
  (P50/P95 latency, cumulative cost per scenario, exact + semantic cache hit rate, mean
  fact-tracker recall), the three curves as Markdown tables (latency vs `tokens_in`;
  cumulative `cost_usd` vs `turn_index`; `MemoryDriftMetric` vs N), and **two analysis
  paragraphs**, each carrying at least one specific quantitative claim
  (e.g. "recall for `project_name` drops below 60% at N = 12" / "cost at turn 20 is
  X.X× the cost at turn 1").

- **D9 — Report justifications.**
  In `REPORT.md`, justify (i) the metrics location (`evals/stress/metrics.py`) and
  (ii) the `turn_observed` transport (embedded in the response).

- **D10 — Tests.**
  Update `tests/test_stress_metrics.py` for the new `MemoryDriftMetric` signature; keep
  the required "one passing / one failing / one edge-case" coverage. Add a scenario
  truncation test (`tests/test_stress_runner.py` or a new file).

- **D11 — (decision) ACB observation.** The brief only requires
  `/sessions/{id}/estimate`. Optionally add `TurnObservation` to `ACBResponse` for
  symmetry. **Decision D14.**

- **D12/D13/D14 — see §6 Open decisions.**

---

## 3. Ordered implementation steps

Each step ends with a concrete check. **No step starts until this plan is approved.**

1. **D1** — `MemoryDriftMetric(fact, where=[...])`.
   *Check:* `uv run pytest tests/test_stress_metrics.py -q` green.
2. **D2** — scenario truncation helper + relaxed invariant.
   *Check:* construct `truncated(3)` / `truncated(20)` for each profile; assert lengths.
3. **D3 + D5 + D6** — runner: `--turns` loop, per-turn fact recall, cache columns.
   *Check:* a dry run with `--scenarios growing --attachment-sizes 0 --repeats 1
   --turns 1,3` against an in-process fake produces a well-formed CSV.
4. **D4** — `MAX_ATTACHMENT_CHARS = 60_000` + docs.
   *Check:* `uv run pytest -q` still green; `.env`/compose unaffected (no key change).
5. **D10** — tests for the above.
   *Check:* `uv run pytest -q` (whole suite) green.
6. **D7** — real run (§5). *Check:* `results.csv` exists with ≥ 50 data rows.
7. **D8 + D9** — fill `REPORT.md`. *Check:* no `_xxx_` placeholders remain; two
   paragraphs each contain a numeric claim.
8. **Spec + docs** — add `specs/009-session-6-stress-and-report.md`; update
   `AGENTS.md`/`README.md` run instructions and the `MISSING.md` checklist.

---

## 4. Files touched (forecast)

```
app/config.py                      D4  (MAX_ATTACHMENT_CHARS 20k -> 60k)
.env.example                       D4
evals/stress/scenarios.py          D2  (truncation / N)
evals/stress/metrics.py            D1  (MemoryDriftMetric signature)
evals/stress/run.py                D3, D5, D6  (turn loop, fact recall, cache cols)
evals/stress/REPORT.md             D8, D9  (real numbers + analysis)
tests/test_stress_metrics.py       D10
tests/test_stress_runner.py        D10
specs/009-*.md                     new
AGENTS.md, README.md, MISSING.md   docs
evals/stress/fixtures/*.pdf        generated (gitignored)
evals/stress/results.csv           generated (gitignored)
```

Nothing in the application request/response path changes except the `MAX_ATTACHMENT_CHARS`
default; the 13-field `turn_observed` already ships.

---

## 5. The real run — cost, time, prerequisites

**Grid (brief formula):** `scenarios × sizes × repeats × turns`.
Full grid `3 × 5 × 3 × 20 = 900` turns.

**Measured cost** (live run earlier this session, DeepSeek `deepseek-flash`,
`app/services/llm_wrapper.py::MODEL_COSTS`): a conversational turn was
**$0.0007–$0.0055** (grows with context; mean ≈ **$0.002**), plus the metadata-fallback
call (~$0.0003). At ≈ $0.0023/turn:

- **Full grid** 900 turns → **≈ $2.1** and **≈ 2–3 h** wall-clock. Not recommended.
- **Reduced** `3 scenarios × 5 sizes × 1 repeat × 20 turns = 300` turns → **≈ $0.7**,
  **≈ 40–60 min**. Meets the ≥ 50-row floor with a large margin and still exercises
  every scenario × size cell. **Recommended default.**
- **Minimal** `3 × 5 × 1 × 5` (N=1,3,6,10,20 only) → **≈ $0.45**, but recall-vs-N is
  thinner.

The brief explicitly allows "adjusting the floor to get it to the minimum", so the
reduced grid is defensible. **The run needs your explicit go-ahead and a budget ceiling**
(the earlier $0.10 cap is far below any of these).

**Prerequisites before the run:**
1. `uv run python -m evals.stress.fixtures.build_pdfs` (needs `fpdf2`, already declared;
   run `uv sync` first — `uv.lock` was refreshed but PDFs are generated on demand).
2. **Aux models must actually work**, or compression/metadata silently degrade (proven
   live earlier: `summarizer_failed` / `metadata_extraction_failed` /
   `critic_failed_fallback_accept` because `gpt-4o-mini` has no credits and the OpenAI
   free tier is still returning `credit_balance_exhausted`). For a meaningful run, point
   `COMPRESSION_MODEL` and `METADATA_EXTRACTOR_MODEL` at a working provider
   (`deepseek/deepseek-flash`) **or** get the OpenAI free tier active. This is a `.env`
   change + `docker compose up -d --force-recreate api`; it is a **decision** (§6 D15).
3. The API reachable at `http://localhost:8000` (compose is up now).

**Cost accounting caveat:** `TurnObservation.cost_usd` is computed from `MODEL_COSTS`
(the price table), so it reports a non-zero figure even where real spend is $0 (free
tier). The report's cost curve is therefore a *modelled* cost, not an invoice — state
this in `REPORT.md`.

---

## 6. Open decisions for you

- **D12** — Set `MAX_ATTACHMENT_CHARS = 60_000` (brief-exact) or keep `20_000` (our
  documented choice)? The brief's 50/100 KB data points only separate at 60 000.
- **D13** — Cache hit-rate columns: **(a)** report 0 % + justify (conversational never
  caches) or **(b)** add a live cached-endpoint probe for real numbers?
- **D14** — Add `observation` to `ACBResponse` too, or leave ACB out of this phase?
- **D15** — Which provider for `COMPRESSION_MODEL` / `METADATA_EXTRACTOR_MODEL` during the
  run: DeepSeek (works now), or wait for the OpenAI free tier? Without this the
  "recall vs N" curve is meaningless (summaries never get written).
- **D16** — Run size: reduced grid (≈ $0.7 / ~1 h) — confirm, or a different budget.

---

## 7. Explicit non-goals (this phase)

- No change to the estimation prompt content or the ACB/critic logic.
- No new endpoints.
- No RAG implementation — the report only *argues* for it from the data.
- No Streamlit UI work.
- No unification of the two pricing tables (still a spec-001 follow-up).
