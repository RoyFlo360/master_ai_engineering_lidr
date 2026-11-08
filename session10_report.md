# Session 10 report — hybrid search and cross-encoder reranking

- **Run date:** 2026-11-07/08 (local).
- **Machine:** Windows host, Docker Desktop 29.9.2; the API ran in its Linux
  container, the harness ran on the host against `localhost:5433`.
- **Corpus state:** `data/budgets_sample.json`, 17 budgets / 60 `budget_component`
  chunks, ingested with `pseudonymize: false` on 2026-11-08. No other corpus was
  present.
- **Embedding model:** `text-embedding-3-small` (1536 dims), `k = 5`.
- **Implementation:** spec 016 (this session). Retrieval lives in
  `app/generation/rag/retrieval/`; the corpus half is untouched.

Every number below is copied from one run's `var/s10_eval.json`. Nothing was
hand-adjusted, and no configuration was re-tuned after the fact to improve its
score.

---

## 1. Golden set

`evals/golden_retrieval.json` — five project descriptions to be estimated, each
hand-annotated with the historical budgets that are genuine analogues. Adopted
from the course reference, re-checked id-by-id against our corpus (all ids
present, all 17 `project_summary` values identical), and every borderline
inclusion is named in the file's `note` fields rather than silently kept.

A retrieved chunk counts as relevant iff its parent `budget_id` is in the query's
`relevant_budget_ids`. Duplicate budgets count once, so a query cannot score above
1.0 by retrieving five chunks of one relevant budget.

| Query | Topic | Relevant budgets |
| --- | --- | --- |
| Q1 | Mobile banking, OAuth2, PSD2, ledger | 001, 003 |
| Q2 | Headless e-commerce storefront, catalog, recommendations | 005, 006, 007, 017 |
| Q3 | Telemedicine, appointments, HL7/FHIR | 009, 010 |
| Q4 | Industrial IoT telemetry, predictive maintenance | 013, 015 |
| Q5 | Real-time payments, fraud, reconciliation | 003, 001 |

---

## 2. The four configurations

| Config | Search | Rerank | Precision@5 | Mean latency (ms) |
| --- | --- | --- | --- | --- |
| A | vector | no | **0.360** | **259** |
| B | hybrid | no | **0.360** | **253** |
| C | vector | yes | 0.320 | 9,408 |
| D | hybrid | yes | 0.320 | 9,459 |

Per query:

| Query | A | B | C | D |
| --- | --- | --- | --- | --- |
| Q1 | 0.40 | 0.40 | 0.20 | 0.20 |
| Q2 | 0.40 | 0.40 | 0.40 | 0.40 |
| Q3 | 0.20 | 0.20 | 0.20 | 0.20 |
| Q4 | 0.40 | 0.40 | 0.40 | 0.40 |
| Q5 | 0.40 | 0.40 | 0.40 | 0.40 |

Method: one discarded warm-up per (query, configuration), then three measured
runs averaged. Latency is end-to-end retrieval — embed, SQL, fusion, rerank — so
the reranker's cost is inside the number rather than hidden beside it.

---

## 3. What the numbers actually say

### 3.1 Hybrid search changed nothing on this corpus

A and B are identical on every query. That is not a bug in the fusion: the
lexical branch does fire, but on 60 chunks it returns almost exactly the chunks
the vector branch already returned — a hybrid run evaluated 51 candidates where
the vector run evaluated 50. With 17 budgets represented by 60 chunks, dense
retrieval already reaches every plausible candidate, so adding a second ranking
cannot reorder a top-5 that the first ranking already gets right.

The honest reading: **this corpus is too small to show what hybrid search is
for.** Lexical search earns its keep when the corpus has identifiers, rare terms
or a dominant document type that floods the dense top-k. None of those hold at 60
chunks. The Postgres test that proves the branches *can* disagree
(`tests/test_retrieval_postgres.py`) is therefore the real evidence that the
mechanism works; the golden set cannot demonstrate a lift it does not have.

### 3.2 Reranking cost 36× and did not improve precision@5

C and D are identical to each other and marginally *worse* than A and B on this
metric. The cost is unambiguous: ~9.4 s versus ~0.26 s per query, on CPU, for 50
candidate pairs — 188 ms per pair on this machine.

Two facts have to be reported together, because either alone misleads:

**The reranker is working.** Scoring Q1's candidates directly shows it separates
the right budget from the wrong ones by a wide margin:

```
 10.599   0.284  BUD-2024-001   (the exact match: mobile banking, OAuth2, PSD2)
  9.681   0.294  BUD-2024-001
  7.839   0.316  BUD-2024-001
 -8.871   0.534  BUD-2024-002   (loan origination: wrong domain)
 -9.331   0.516  BUD-2024-003   (payments gateway: adjacent, not the same build)
```

**The metric hides the improvement.** On Q4 the unranked run retrieved
`013, 015, 014` — 014 is the deliberate distractor (warehouse AGV coordination,
industrial but not telemetry). The reranked run retrieved `013, 015`: it *removed
the false positive*. Precision@5 stayed 0.40 because the metric counts hits out of
five slots, and both runs filled five slots; the improvement is invisible.

Conversely on Q1 the reranker concentrated all survivors on budget 001, which
dropped 003 from the list and cost 0.20. Ranking five chunks from one budget is
arguably *better* estimation evidence — you want depth on the closest analogue —
but precision@5 over distinct budgets rewards spreading across budgets. The two
readings disagree, and the table only shows one of them.

Measured side by side, on the same run:

| Config | Precision@5 (brief's metric) | Precision over distinct budgets retrieved |
| --- | --- | --- |
| A | 0.360 | 0.833 |
| B | 0.360 | 0.833 |
| C | 0.320 | 0.800 |
| D | 0.320 | 0.800 |

Neither metric shows a reranking win. What the second column shows is that
retrieval is *already* precise — roughly four of every five distinct budgets it
returns are correct. The headroom reranking would need to exploit does not exist
at this corpus size.

### 3.3 A latency caveat that is about this machine, not the model

188 ms per pair is slow for a MiniLM cross-encoder; on a normally-provisioned
CPU on the same host it is typically an order of magnitude faster. Part of the
9.4 s is container/CPU-thread overhead on this setup. **Do not quote 9.4 s as
"the cost of reranking"** — quote it as "what it cost here", and re-measure before
making a capacity decision.

---

## 4. Conclusions

**Which configuration would I choose?** **A — vector, no reranking.** It matches
the best precision available on this corpus, costs a quarter of a second, and has
no model download, no CPU sink and no new failure mode. B is the same result for
the same cost, so it is not wrong, but it carries a second query path for zero
measured benefit. C and D are strictly dominated here: equal-or-worse precision
for ~36× the latency.

**Does reranking justify its latency in this use case?** **Not at this corpus
size, and not on this evidence.** The mechanism is sound — it correctly ranks the
true analogue above the distractors, and on Q4 it deleted a false positive that
the dense retriever kept. But the estimator already retrieves about 83% correct
budgets among distinct results, so there is little for a cross-encoder to fix, and
the bill is three orders of magnitude in latency. Reranking becomes worth
measuring again when the corpus grows by an order of magnitude and the dense
top-k starts containing genuine near-misses; at that point configuration D is the
one to re-test, not C, because hybrid search is what supplies the recall the
reranker needs.

**What this session does not show.** It does not show that hybrid search or
reranking are wrong ideas — it shows that a 60-chunk corpus cannot demonstrate
their value, and that the brief's precision@5 metric cannot see the improvement
the reranker actually produced. Both are arguments for keeping the feature behind
a switch (which it is: `RETRIEVAL_SEARCH_MODE` and `RERANKER_ENABLED`, both off by
default) rather than wiring it into the default path.

---

## 5. Reproducing this

```powershell
docker compose up -d
docker compose exec api python scripts/query_examples.py      # idempotent ingest
$env:ESTIMATOR_API_BASE_URL = "http://localhost:8000"
$env:DATABASE_URL = "postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run python scripts/eval_retrieval_s10.py --json var/s10_eval.json
```

`--dry-run` prints the configurations and the golden set without touching a
network or a database. The reranked configurations download ~120 MB of weights on
first use; run the gate once per container lifetime:

```powershell
docker compose exec api python -m app.generation.rag.retrieval.verify_reranker
```

---

## 6. The grounded-estimation chain (T0B)

Session 10's brief forbade query expansion, multi-index routing and metadata
filtering, but the approved scope (decision C3) also included the Session-9 live
layer that nothing in this repo had: the chain that turns retrieved chunks into an
estimate. It shipped as spec 017, and it closes the gap `arquitectura-actual.md`
diagnosed — retrieval now has a consumer.

### What it does

```
transcript → reformulate → retrieve (with a distance floor) → assemble
           → generate → validate citations (one retry) → response
```

Five routes under `/v1/estimate`: `/from-transcript` plus four `/stages/*`
endpoints that expose each step so it can be inspected by hand.

### Live runs (real provider, real corpus)

**The ambiguous transcript** (`examples/transcripts/02_ambiguous.txt`, 2,898 chars
of Spanish):

- Reformulation, 1.9 s: `source=llm`, `confidence=0.9`, and the search string came
  back in English — `"e-commerce platform, customer loyalty features, dashboard
  for order management, secure card payment, email notifications"`. Translation is
  part of the job here: the transcripts are Spanish, the corpus is English.
- Full chain, 8.5 s: `grounded=true`, **5 citations**, confidence 45%, four
  modules, 470 hours. Every citation resolved to a real budget — three of them
  cited `BUD-2024-017` (the e-commerce MVP), which is the correct analogue for a
  gourmet shop wanting a storefront, loyalty, a dashboard, card payments and an
  order email.

**Idempotency:** the same request twice with `idempotency_key` returned
`cached=false` then `cached=true`, identical estimate, one generation call.

### The defect this surfaced

A deliberately unrelated brief ("satellite imagery of coral reefs") came back
**`grounded=true`** on five irrelevant finance budgets, confidence 4%. The flag
was lying: `/v1/retrieval/search` returns the `k` nearest chunks whatever the
distance, so "nothing relevant exists" is not distinguishable from "here is your
answer" without a floor.

Fixed by adding `RETRIEVAL_DISTANCE_THRESHOLD = 0.65`, applied by the **chain
only** — the retrieval endpoint still returns the nearest chunks, because a caller
inspecting retrieval wants to see them. Calibrated on this corpus the same day:
relevant queries land at 0.43–0.60, the unrelated topic at 0.71–0.72.

After the floor, the same unrelated brief returns `grounded=false`,
`confidence_pct=0`, `citations=[]`, `retrieved=0` — **and the LLM is never called**,
which is the whole point: a model answering from memory is the failure mode this
layer exists to prevent. The ambiguous transcript still returns `grounded=true`
with 5 citations, so the floor separates the two cases rather than suppressing both.

---

## 7. Test evidence

| Check | Result |
| --- | --- |
| `uv run pytest -q` | 813 passed, 24 skipped, 3 warnings |
| `uv run pytest -q -m postgres` (live DB) | 24 passed, corpus intact afterwards |
| Migration round-trip on real Postgres | `upgrade head` → `downgrade -1` → `upgrade head`; `content_tsv` and its GIN index appear, disappear, return; `ix_chunks_embedding_hnsw` survives throughout |
| `content_tsv` backfill | 60/60 existing rows populated by the generated column, no backfill script |
| Reranker gate | exit 0, relevant 10.78 vs irrelevant −8.55 |
| Reranker laziness | importing the module leaves `torch` and `sentence_transformers` out of `sys.modules` |

### Mutation testing — ran, and the 90% gate FAILS

The harness was made to work by running it inside the Linux `api` container (it
refuses native Windows: *"to run mutmut on Windows, please use the WSL"*,
upstream issue #397). Three configuration defects had to be fixed first, all
recorded in `pyproject.toml`'s comments: mutmut 3.8.1 wants `source_paths`, not the
deprecated `paths_to_mutate`; it copies **only** the mutated files, so `also_copy`
must name `app`, `data`, `evals`, `scripts` and `examples` or every import in the
sandbox fails; and the test selection must be the retrieval suites, because the
full suite includes docs-link tests that cannot pass in a partial copy.

Command:

```bash
docker compose exec api sh -c "cd /srv/app && uv run mutmut run"
```

Result — **557 mutants**:

| Outcome | Count |
| --- | --- |
| killed | 262 |
| survived | 157 |
| no tests (unmapped) | 137 |
| timeout | 1 |

**Kill rate: 47% of all mutants, 62% of the 420 mutants that had a test mapped.**
The target was ≥ 90%. **The gate is not met.**

Survivors by module:

| Module | Surviving mutants |
| --- | --- |
| `app/embedding_pipeline/store/repository.py` | 136 (all in `search_lexical`) |
| `app/generation/rag/retrieval/pipeline.py` | 119 |
| `app/generation/rag/retrieval/reranker.py` | 36 |
| `app/generation/rag/retrieval/fusion.py` | 4 |

What the survivors say, and why this is a useful result rather than a failed run:

1. **`search_lexical` has no hermetic test at all.** Its 137 unmapped mutants are
the whole function — it is covered only by `tests/test_retrieval_postgres.py`,
   which is deselected here because it needs `TEST_DATABASE_URL`. A SQL-building
   function with no unit test is the largest single hole in the suite.
2. **The pipeline survivors are concentrated in private helpers** — `_field`,
   `_as_float`, `_chunk_from_row`. They are exercised indirectly through the four
   configuration tests, so a mutation only shows up if it changes the final
   ordering, and many coerced-field tweaks do not. Direct unit tests on those
   helpers would kill most of the 119.
3. **`fusion.py` has only 4 survivors out of its mutants**, which is the shape you
   want: the pure-function tests are doing their job.

This is the measurement the gate exists to produce. It is **not** a claim that the
retrieval code is untested — 813 tests pass — but it does show the tests pin
behaviour rather than implementation detail in exactly one place (the three pure
files) and only pin outcomes in the rest.

**To close it:** add Postgres-marked coverage to the mutation test selection (or a
hermetic SQL-shape test for `search_lexical`), and add direct unit tests for the
three pipeline helpers. Neither is done here.

---

## 8. Known gaps

1. **The mutation gate fails.** 47% of mutants killed (62% of testable ones) against
   a 90% target — see the table above. The two causes are named and fixable: no
   unit test covers `search_lexical`, and the pipeline's private helpers are only
   exercised indirectly.
2. **The T0B evaluation is two anecdotes, not a measurement.** The grounded chain
   was run twice (one ambiguous transcript, one unrelated brief) and inspected by
   hand. There is no golden set for *estimation quality* — no scoring of whether the
   470-hour figure or its module breakdown is right. The Session-10 brief does not
   ask for one, but "it produced citations" is weaker evidence than the retrieval
   table above.
3. **The corpus is small.** Every conclusion is scoped to 17 budgets / 60 chunks;
   none of it generalises to a production-sized corpus without re-measurement.
4. **Latency is machine-specific.** The 9.4 s rerank figure includes unusually
   slow per-pair scoring on this host; the 8.5 s chain figure is the same hardware.
5. **The metric is coarse.** Precision@5 over distinct budgets cannot express
   "removed a false positive" or "concentrated on the best analogue"; both
   happened and both were invisible. A judged-relevance or NDCG metric would see
   them, and is explicitly out of scope for this session.
6. **The reranker's multilingual mismatch** was not tested with Spanish queries;
   all five golden retrieval queries are English like the corpus. T0B does exercise
   the Spanish→English path, but only through one transcript.
7. **Idempotency is in-process.** It dies on restart; Redis is the production
   answer and is deliberately not built here.
