# 002 — Usage and cost reporting, including cache hits

- **Status:** Implemented
- **Date:** 2026-09-27
- **Scope:** `app/schemas/new_estimation_schema.py`, `app/services/llm_wrapper.py`,
  `app/routers/estimations.py`, `streamlit_app_loader.py`
- **Supersedes / Superseded by:** extends the contract in
  [004](004-prompt-v1-estimation-endpoint.md)

## Problem

`POST /api/v1/new-estimate` returned `{text, prompt_version}` and nothing else. The
service had already paid for the call and knew exactly what it cost, but the client
could not show it — `streamlit_app_loader.py` had nothing to put in its sidebar,
while the older `streamlit_app_estimation_api.py` showed tokens and cost inline for
`/estimate`. The two clients disagreed about what the user could see.

The non-obvious half: what should a **cache hit** report? The obvious answer ("zero
tokens, zero cost", which is what the streaming path does) throws away the token
counts of an answer the user is looking at, and the other obvious answer (report the
stored cost) claims a spend that did not happen.

## Goals

- `POST /api/v1/new-estimate` reports the token counts and the cost of the call that
  produced `text`.
- A replay from the cache is visibly free — `Cache hit, $0 USD` in the sidebar — while
  still showing the token counts of the answer.
- The client renders the numbers; it does not compute them. Whether a call was billed
  is knowledge the API has and the client does not.

## Non-goals

- Cumulative totals across calls — that is [006](006-running-usage-totals.md).
- Changing the streaming path. `/estimate/stream` keeps its own semantics (below).
- Any notion of billing, budgets or alerts.

## Design

**One usage shape.** `TokenUsage` (`input_tokens`, `output_tokens`, `total_tokens`,
`cost_usd`) already existed for `/estimate`; the typed response reuses it rather than
inventing a parallel shape, so both clients read the same field names.

**The pricing rule lives with the pricing.** `_usage_from_result(result)` was added to
`app/services/llm_wrapper.py`, beside the streaming path's `_usage_payload`, rather
than inline in the router:

```python
"cost_usd": 0.0 if result.get("cache_hit") else result.get("cost_usd", 0.0),
```

The token counts are passed through from the cached entry on a hit. That is not an
oversight: a cache hit means the *system prompt and user message were byte-identical*,
and identical text tokenises identically — the counts are a property of the prompt,
not of the request that happened to trigger it. The cost is a property of the call, so
it resets to zero.

**The two flows deliberately differ.**

| | `/new-estimate` (blocking) | `/estimate/stream` (SSE) |
| --- | --- | --- |
| cache-hit tokens | the cached counts | `0` |
| cache-hit cost | `$0.000000` | `$0.000000` |

Both are "actual spend is zero". The streaming client scores a run by what it cost, so
zeros read correctly there; the typed client shows an answer and its usage, where zero
tokens next to real text would look like a bug. Recorded here because the difference
looks like an inconsistency to anyone reading only one path.

**Rejected:** zeroing the cost in the Streamlit client (the API is the only place that
knows a replay happened); echoing the cached cost (would overstate the total in 003).

## Contract

```
POST /api/v1/new-estimate
{"description": "…", "project_type": "mobile_app", "detail_level": "medium",
 "output_format": "phases_table"}

→ 200
{
  "text": "| phase | duration_weeks | …",
  "prompt_version": "v1",
  "cache_hit": false,
  "usage": {"input_tokens": 2063, "output_tokens": 812,
            "total_tokens": 2875, "cost_usd": 0.000797}
}
```

The same request served from the cache returns `"cache_hit": true` with the same
`input_tokens` / `output_tokens` / `total_tokens` and `"cost_usd": 0.0`.

## Verification

- `uv run pytest tests/test_estimation_v1.py tests/test_llm_wrapper.py -q` → 26 passed
  (18 + 8).
- `test_new_estimate_returns_the_estimation_with_its_usage` asserts the entire body,
  so a stray or missing field fails.
- `test_a_cache_hit_reports_the_cached_tokens_at_zero_cost` asserts tokens unchanged
  and `cost_usd == 0.0`.
- `test_usage_from_result_keeps_the_tokens_of_a_replay_but_not_its_cost` and
  `test_usage_from_result_charges_a_cache_miss_what_the_call_cost` pin the rule itself.
- **Live, over HTTP, against the container** — the discriminating check: a cache entry
  was seeded with `cost_usd: 0.000797` (a *non-zero* stored cost) and the payload
  replayed. The response reported `cost_usd: 0.0` while the tokens came back intact
  (`2063 / 812 / 2875`), which proves the zeroing happens on the hit instead of the
  stored value being echoed.
- **The sidebar rendering** was checked by driving the real
  `streamlit_app_loader.py` headlessly with Streamlit's `AppTest`: the sidebar showed
  `Cache hit, $0 USD` plus `Input tokens = 2063`, `Output tokens = 812`,
  `Total tokens used = 2875`, `Cost in USD = $0`.

Not verified: a cold (billed) call end to end — it spends credit, so the owner runs
those. Every test stubs the wrapper.

## Follow-ups

- **Rounding is settled at 6 decimals.** `_estimate_cost` rounds each call, and the
  totals in [006](006-running-usage-totals.md) round their sum the same way. A
  4-decimal rounding was tried on 2026-09-27 and reverted the same day: at 4 dp a call
  cheaper than ~$0.00005 reads as `$0.0000` and disappears from a total, and it left
  `tests/test_estimations_api.py::test_stream_reports_usage_and_cost_after_the_tokens`
  (expects `0.000797`) red. Display precision belongs in the client
  (`format_cost` in `streamlit_app_loader.py`), not in the pricing.
- The streaming path reports zero tokens on a hit; if a client ever needs the counts
  there too, `complete_stream`'s replay branch has them in `cached["usage"]`.
