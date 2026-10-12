# CAG Stress Test — Report

> Empirical map of where this CAG breaks: at which turn memory drifts, how
> latency and cost grow with context and attachment size, and what edge case would
> justify a RAG switch. Produced by `uv run python -m evals.stress.run` against a
> real estimator (`http://localhost:8000`), real LLM, real PDFs.

**Run parameters**

| Setting | Value |
|---|---|
| Main model | `deepseek/deepseek-flash` (`PRIMARY_MODEL` default) |
| Aux models | `gpt-4o` (metadata) · `gpt-4o-mini` (summarizer, critic), `deepseek/deepseek-flash` fallback |
| Scenarios | growing, pivot, contradiction |
| Attachment sizes (KB) | 0, 5, 20, 50, 100 |
| Repeats per cell | 1 |
| Turns walked | Tier A: 5 · Tier B (size 0): 20 |
| Latency budget | 8000 ms · Cost budget/turn: $0.02 |
| Rows in `results.csv` | **135** (75 + 60) |
| Observed cost | Tier A $0.3514 + Tier B $0.2522 = **$0.6036** |
| Wall clock | Tier A ~8 min · Tier B ~13 min |

**Design decisions (justifications the brief asked for).**

- **Metrics location — `evals/stress/metrics.py`, not `evals/metrics.py`.** The
  three stress metrics consume a `TurnObservation` and a `GET /sessions/{id}`
  snapshot, not the `(GoldenCase, EstimationResult)` pair that `evals/metrics.py`
  is built around. Different input types justify a sibling module; `MetricResult`
  is reused verbatim so the report shape is uniform.
- **`turn_observed` transport — embedded in the POST response, not scraped from
  stdout.** `StructuredEstimationResponse.observation` carries the 13 fields, so the
  runner reads `response["observation"]` directly: no log parsing, no timestamp
  reconciliation, and the event is also emitted as a single structlog line for
  `docker compose logs | grep turn_observed`.
- **N dimension — one walk yields every N.** Turn *k*'s telemetry depends only on
  turns `1..k`, so walking `max(N)=20` produces the N=1,3,6,10,20 observations as
  its rows; the report groups by `turn_index`. Running a fresh session per N would
  have cost ~4× for identical data.
- **Two tiers instead of the full grid (135 rows, $0.60).** Cost grows with context:
  a 20-turn session costs ~$0.06–0.10, so the literal `3×5×3×20` grid was ~$2.5.
  Tier A sweeps all 5 attachment sizes at 5 turns (the size curves); Tier B walks
  size 0 to 20 turns (the N curves). Both exceed the ≥50-row floor.

---

## 1. Summary table

One row per scenario × attachment size (Tier A, 5 turns each). **Cache hit rates are
0% by design** on the session endpoint — it bypasses both caches; see §4 for the real
cache measurement on `/api/v1/new-estimate`.

| Scenario | KB | n | P50 ms | P95 ms | Total $ | Exact | Semantic | Recall % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| growing | 0 | 5 | 7123 | 7421 | 0.0111 | 0% | 0% | 100.0% |
| growing | 5 | 5 | 4070 | 4472 | 0.0078 | 0% | 0% | 93.8% |
| growing | 20 | 5 | 7313 | 7981 | 0.0248 | 0% | 0% | 93.8% |
| growing | 50 | 5 | 4655 | 8534 | 0.0384 | 0% | 0% | 91.7% |
| growing | 100 | 5 | 4488 | 6160 | 0.0445 | 0% | 0% | 85.4% |
| pivot | 0 | 5 | 3560 | 6707 | 0.0080 | 0% | 0% | 100.0% |
| pivot | 5 | 5 | 4292 | 4503 | 0.0128 | 0% | 0% | 100.0% |
| pivot | 20 | 5 | 4054 | 4601 | 0.0179 | 0% | 0% | 100.0% |
| pivot | 50 | 5 | 2781 | 3231 | 0.0244 | 0% | 0% | 100.0% |
| pivot | 100 | 5 | 7962 | 8104 | 0.0633 | 0% | 0% | 100.0% |
| contradiction | 0 | 5 | 3465 | 6206 | 0.0070 | 0% | 0% | 66.7% |
| contradiction | 5 | 5 | 4057 | 6508 | 0.0108 | 0% | 0% | 72.9% |
| contradiction | 20 | 5 | 7055 | 7336 | 0.0261 | 0% | 0% | 70.8% |
| contradiction | 50 | 5 | 3698 | 4049 | 0.0254 | 0% | 0% | 64.6% |
| contradiction | 100 | 5 | 4297 | 4486 | 0.0292 | 0% | 0% | 100.0% |

**Read:** cost is dominated by attachment size, not scenario — `growing` goes from
$0.0111 (0 KB) to $0.0445 (100 KB), a **4.0×** rise for the same 5 turns. The
`contradiction` profile has the lowest recall (64.6–100%) because its facts
(mutating budget figures) are the ones most likely to be summarised away.

---

## 2. Three curves

### 2a. Latency vs context size (`latency_ms` vs `tokens_in`)

| `tokens_in` bucket | Median latency (ms) | n |
|---|---:|---:|
| 1000–2999 | 3305 | 6 |
| 3000–6999 | 3774 | 25 |
| 7000–14999 | 4472 | 29 |
| 15000+ | **8331** | 75 |

Median latency **2.5×** as context crosses 15 k input tokens (3305 ms → 8331 ms); the
15 k+ bucket exceeds the 8000 ms budget.

### 2b. Cumulative cost (USD) vs turn (size 0, per scenario)

| Turn | growing | pivot | contradiction |
|---:|---:|---:|---:|
| 1 | 0.0007 | 0.0007 | 0.0007 |
| 5 | 0.0066 | 0.0086 | 0.0066 |
| 10 | 0.0281 | 0.0342 | 0.0391 |
| 15 | 0.0454 | 0.0629 | 0.0636 |
| 20 | 0.0591 | 0.1036 | 0.0895 |

Cost is **super-linear**: the first turn costs $0.0007, a 20-turn session $0.059–0.104
— **84× to 148×** the turn-1 cost — because every turn re-injects the growing window
plus the rolling summary.

### 2c. Fact-tracker recall vs N (size 0)

| N | mean recall % | | N | mean recall % |
|---:|---:|---|---:|---:|
| 2–6 | 100.0% | | 13 | 72.2% |
| 7 | 94.4% | | 14 | 66.7% |
| 8 | 81.0% | | 15 | 69.0% |
| 9 | 75.0% | | 16 | 66.7% |
| 10 | 74.1% | | 17 | **62.5%** |
| 11 | 73.3% | | 18 | 64.7% |
| 12 | 69.7% | | 19 | 64.8% |
| | | | 20 | 66.7% |

The turn-1 fact alone (`project_name`: Nimbus / Helios / Atlas) survives **100%** at
every N — it lives in `metadata.project_name` and is never evicted. The **aggregate**
recall is what decays: recall is perfect through N=6, first drops at N=7 (94.4%),
crosses below 75% at N=9, and settles near **63–67% by N=17–20**.

---

## 3. Analysis — where the CAG breaks, and why

**Where it breaks.** Memory, not latency or cost, is the first thing to go. Recall is
100% for the first six turns because the entire conversation still fits
(`MAX_CONVERSATION_TURNS=6`); at **N=7 the sliding window evicts turn 1**, compression
kicks in (the summary first appears at turn 7 with ~900 characters), and recall
immediately drops to 94.4%. It falls under 75% by **N=9** and plateaus around
**63–67% by turn 17** — the summariser keeps the gist but loses individual trackable
facts (a technology name, an "audit log" requirement, a CSV-import ask). The
interesting twist: the *project name* survives every turn because it is promoted to
`metadata.project_name`, while everything else that only ever lived in the raw turns
degrades. Cost and latency degrade more slowly and more predictably: latency roughly
follows input tokens (median **8331 ms once past 15 k tokens**, i.e. over an 8 s SLA),
and cumulative cost is **84–148× the turn-1 cost by turn 20**. So the ordering is
**memory first (≈N=7), then latency (≈15 k tokens), then cost (unbounded)**.

**The edge case that justifies RAG.** The data says the break is *context volume per
turn*, driven by two things re-sent in full every turn: the conversation and the
attachment. In the `growing`/`pivot` profiles the **100 KB attachment costs 4× the
no-attachment turn** and injects ~15 k tokens *each turn* — yet most of that text is
never used. The recall curve (63–67% floor) is the sharper signal: once the window
evicts a turn, its facts are only as good as the summariser, and a *later* question
about them cannot be answered reliably. The switch to RAG is justified exactly at
that intersection: **a conversation longer than ~N=10 turns, or one carrying
attachments ≥ 20 KB, where a later turn needs a specific early fact.** Then
retrieving the 2–3 k relevant tokens on demand beats re-injecting 15 k+ tokens every
turn — it flattens the latency and cost curves (§2a/2b) and removes the recall cliff
(§2c), because the fact is fetched from the source rather than hoped-for in the
summary.

---

## 4. Cache probe (`/api/v1/new-estimate` — the cached path)

The session endpoint never caches, so its hit rate is structurally 0%. The real cache
was measured separately (`results.cache-probe.csv`):

| Probe | `cache_hit` | cost |
|---|---|---|
| cold | False | $0.000412 |
| exact replay (identical body) | **True** | $0.000000 |
| near-duplicate paraphrase | False | $0.000498 |

**Exact cache hit rate: 100%** (a replay costs nothing). **Semantic cache hit rate:
0%** — the paraphrase did not clear the 0.92 cosine threshold, which is the intended
conservative default, not a failure.

---

## 5. Reproducibility

```bash
# Deterministic PDF fixtures (5/20/50/100 KB extracted text).
uv run python -m evals.stress.fixtures.build_pdfs

# Tier A — attachment curves (all sizes, 5 turns).
uv run python -m evals.stress.run --http http://localhost:8000 \
    --scenarios growing,pivot,contradiction --attachment-sizes 0,5,20,50,100 \
    --repeats 1 --turns 1,3,5 --max-turns 5 --cache-probe \
    --output evals/stress/results.csv

# Tier B — recall/cost vs N (size 0, 20 turns), appended.
uv run python -m evals.stress.run --http http://localhost:8000 \
    --scenarios growing,pivot,contradiction --attachment-sizes 0 \
    --repeats 1 --turns 1,3,6,10,20 --max-turns 20 --append \
    --output evals/stress/results.csv

uv run python evals/stress/analyze.py    # regenerates the tables above
```

`results.csv` is gitignored (regenerated per run). This `REPORT.md` is the artefact
that ships.
