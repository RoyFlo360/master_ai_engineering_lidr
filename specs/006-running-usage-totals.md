# 003 — Running usage totals

- **Status:** Implemented
- **Date:** 2026-09-27
- **Scope:** `app/services/usage_ledger.py`, `app/schemas/usage_schema.py`,
  `app/dependencies.py`, `app/routers/estimations.py`, `streamlit_app_loader.py`
- **Supersedes / Superseded by:** —

## Problem

After 002 the sidebar showed what the *last* call cost, and nothing else. Doing three
estimations showed three unrelated numbers — there was no way to see what the session
had spent in total, and a cache hit (free) was indistinguishable from a call that never
happened.

Summing in the client was the obvious fix, and wrong for the stated requirement: the
client's own memory is `st.session_state`, which a browser refresh wipes. The request
was explicitly for a total that survives that.

## Goals

- The sidebar shows the summed cost of every request, cache hits included at $0.
- The total survives a browser refresh or a new Streamlit session.
- The arithmetic lives in one place and is testable without a browser.

## Non-goals

- **Surviving an API restart.** Explicitly out of scope: an in-process total dies with
  the process. Recorded here as a known limit, not an oversight — see Follow-ups.
- Per-user or per-session accounting. The total is for the process, whoever asked.
- Counting the streaming endpoint. `/estimate/stream` does not record (below).

## Design

**`UsageLedger`** (`app/services/usage_ledger.py`) holds a `UsageTotals` dataclass —
`calls`, `cache_hits`, `input_tokens`, `output_tokens`, `total_tokens`, `cost_usd` —
and exposes `record(usage, cache_hit=…)` and `totals()`. A `threading.Lock` guards
both: endpoints run on FastAPI's threadpool, so two requests can interleave, and
`+=` on a dataclass field is not something to rely on.

`record` adds the cost it is given, so a cache hit contributes `0.0` by construction
(the value 002 puts in the response) — the ledger has no opinion about cache pricing,
it just sums what it is told.

**`get_usage_ledger()`** in `app/dependencies.py` is an `lru_cache` singleton, like
`get_cache` and `get_llm_wrapper`, so every request in a process shares one ledger.

**`GET /api/v1/usage`** exposes `totals()` as `UsageTotalsResponse`. The router records
the call *before* building the response, so a serialization failure after the LLM call
still counts the spend that actually happened.

**The client reads, it does not accumulate.** `streamlit_app_loader.py` calls
`fetch_totals()` on every script run — which Streamlit does on every interaction — so
the total is always what the API has, not what this browser session remembers.
Unreachable API degrades to a caption (`HTTPError` → `None`), it does not break the page.

**Rejected:** `st.session_state` accumulation (dies on refresh — the actual complaint);
a Redis counter (deferred, not refused: it is the documented upgrade path);
computing the total from logged calls at read time (more moving parts, same result).

## Contract

```
GET /api/v1/usage
→ 200
{
  "calls": 3, "cache_hits": 1,
  "input_tokens": 6189, "output_tokens": 2436, "total_tokens": 8625,
  "cost_usd": 0.001594
}
```

No parameters, no failure modes beyond transport. `cost_usd` is rounded to six
decimals on the way out, because summing per-call values that were each rounded can
leave float noise.

`calls` counts every recorded request including replays; `cache_hits` counts the
subset that was served from the cache. `cost_usd` is the sum of what was actually
billed.

## Verification

- `uv run pytest tests/test_estimation_v1.py -q` → 18 passed, including:
  - `test_totals_start_empty_before_any_call` — a fresh process reports zeros.
  - `test_totals_sum_the_cost_and_tokens_of_every_call` — two calls, `calls == 2`,
    tokens doubled, `cost_usd == 2 × 0.000797`.
  - `test_a_cache_hit_is_counted_but_adds_nothing_to_the_total` — three calls with one
    replay gives `calls == 3`, `cache_hits == 1`, cost of the two billed calls only.
  Each test gets a fresh ledger through an autouse fixture, because the real one is a
  process-wide singleton that would otherwise leak between tests.
- **Live, over HTTP**: two seeded replays produced
  `{"calls":2,"cache_hits":2,"input_tokens":4126,"output_tokens":1624,"total_tokens":5750,"cost_usd":0.0}`
  — counting and summing confirmed, at zero credit spent.
- **The restart limit, demonstrated rather than asserted**: `docker compose restart api`
  → `GET /api/v1/usage` → all zeros.
- **The worse case, also demonstrated**: touching one file under `app/` (mtime only, no
  content change) made uvicorn reload — `Finished server process [8]` →
  `Started server process [17]` — and the totals were back to zero, while the Redis
  cache survived the same reload. In development, i.e. with `--reload`, the total is
  wiped by **every code save**, not just by a restart.
- **The sidebar**: `AppTest` against the real `streamlit_app_loader.py` rendered a third
  sidebar section showing `Calls = 1 (1 served from cache)`, `Total tokens used = 2875`,
  `Cost in USD = $0`.

Not verified: a non-zero total live — reaching one requires a billed call, so the sum
of non-zero costs is covered by the tests above instead. The client was checked
headlessly (`AppTest`), not in a browser.

## Follow-ups

- **Persistence.** Process-local is the requirement as stated, and the limit is real:
  restart, and every `--reload` in dev, resets it. Redis is already in the stack and
  already holds the cache; a counter key (`HINCRBYFLOAT` per field) behind the same
  `UsageLedger` interface would make the total outlive the process without changing a
  single line of the client. This is the first candidate for a spec 007 if the totals
  need to be durable.
- **Coverage.** Only `/new-estimate` records. `/estimate/stream` costs real money and
  contributes nothing to the total — misleading if anyone reads the total as "all spend".
- **Multi-worker.** The ledger is per process; with more than one uvicorn worker each
  worker reports its own totals. Fine for a single-process dev stack, wrong in
  production — another argument for the Redis version.
