# 002 — Streaming estimation endpoint

- **Status:** Implemented
- **Date:** backfilled 2026-09-27; describes code that predates this folder
- **Scope:** `app/services/llm_wrapper.py` (`complete_stream`),
  `app/routers/estimations.py`, `app/schemas/estimation_schema.py`,
  `streamlit_app.py`
- **Supersedes / Superseded by:** —

## Problem

A cold estimation takes on the order of ten seconds (12.2s measured with thinking off)
before anything can be shown. Waiting for the whole body means a spinner and no
feedback for that entire time, and if the generation fails at the end the user has
watched a spinner for nothing.

Two things had to reach the client: the tokens as they are produced, and — once the
call finishes — what it cost.

## Goals

- Tokens reach the client while the model is still writing.
- The cost and token counts arrive at the end of the same response, not in a second
  call.
- A failure is visible as a frame in the stream, not as a dropped connection or a
  truncated answer that looks complete.
- A replay from the cache is instant.

## Non-goals

- Structured output, resumable streams, or per-token persistence.
- **Two-phase preprocessing and structural validation are deliberately skipped.** The
  router docstring says why: intermediate phase-1 tokens would leak, and validation only
  makes sense over the complete text. The typed flow in
  [004](004-prompt-v1-estimation-endpoint.md) is where validation lives.

## Design

**The event vocabulary lives in the wrapper.** `LLMWrapper.complete_stream()` yields
wire-ready dicts — `{"event": "token", "data": "<delta>"}` per chunk, then one
`{"event": "usage", "data": "<json>"}` — and the router forwards them unchanged. One
module owns the frame names, so the client, the router and the tests cannot drift apart.

**The blocking generator is driven off the event loop.** LiteLLM's client is
synchronous; calling `next()` on the loop would stall every other request for the
duration of the generation, which is exactly the bug streaming is meant to fix:

```python
event = await loop.run_in_executor(None, _next_event)
```

**The stream ends explicitly.** After the last event the router yields
`{"event": "done", "data": "[DONE]"}`; exceptions inside the generator become
`{"event": "error", "data": str(exc)}` and the connection stays open, so the client can
render `[error] …` in place instead of showing a half-written estimation.

**Usage comes from the provider, not from counting.** The request sets
`stream_options={"include_usage": True}`, so OpenAI-compatible providers send one final
chunk carrying usage and no choices; `_extract_delta` returns `""` for it and
`_usage_payload` turns it into the same four-field shape the blocking paths use.

**A cache hit replays in one frame** and reports `_usage_event(model, None)` — **all
zeros**, tokens included, because that client scores a run by what it cost. The typed
flow reports the cached counts instead; the divergence is deliberate and recorded in
[005](005-usage-cost-and-cache-hit-reporting.md).

**A per-request model override bypasses the Router** (`litellm.completion` with explicit
credentials). That path has **no fallback by design** — the caller asked for a specific
model. Requests without an override go through the Router, which does fall back.

`usage` is never a request field. It once was, and every call was rejected with a 422
before the model was ever reached; the schema's docstring records the rule and
`StreamEstimationRequest` has no `usage` field to this day.

## Contract

```
POST /api/v1/estimate/stream
{"transcription": "…", "model": null, "max_tokens": null}   # both optional

→ 200 text/event-stream

event: token
data: ## Inventory Platform

event: token
data: | Task | Hours | …

event: usage
data: {"input_tokens":2063,"output_tokens":812,"total_tokens":2875,"cost_usd":0.000797}

event: done
data: [DONE]
```

- `422` — `transcription` missing or shorter than 50 characters, or `max_tokens`
  outside `1..16000`.
- Failures *after* the stream opens arrive as `event: error` frames with an HTTP 200
  already sent — the status line cannot carry them.

## Verification

- `uv run pytest tests/test_estimations_api.py -q` → 10 passed.
- `test_stream_reports_usage_and_cost_after_the_tokens` parses the frames out of the
  raw SSE text and asserts the usage shape and the exact cost (`2875` tokens → `0.000797`
  from `MODEL_COSTS`).
- `test_stream_uses_the_service_cap_when_the_request_omits_max_tokens` guards the
  fallback to `wrapper.max_tokens`.
- `test_stream_accepts_a_transcription_without_any_usage_field` is the regression for
  the 422-everything bug.
- The frame parser is a test helper (`frame_data`), so the tests assert on the wire
  format rather than on Python objects.
- Measured and recorded in `README.md`: the same body replayed from the cache answers
  in 0.2s with a single `token` frame and all-zero usage, against 3.7s cold with one
  frame per token.

Not verified: the real provider's chunking and its final usage-bearing chunk. Every
test injects a fake wrapper, so `complete_stream`'s own body — Router dispatch, chunk
accumulation, `_extract_delta` — is not executed by the suite (see Follow-ups).

## Follow-ups

- **`complete_stream` has no unit test.** The endpoint tests inject a fake wrapper, so
  the real method is only ever exercised against a live provider in manual runs. The
  cheapest fix is a test that feeds it a fake chunk iterator, the way `FakeLLM` feeds
  `llm_service`.
- **The streaming path does not record into the usage ledger**
  ([006](006-running-usage-totals.md)), so the running total silently ignores spend from
  this endpoint.
- The cache hit yields the whole estimation as a single `token` frame — fine for the
  UI, but a caller expecting incremental deltas gets one large one.
- Model-override requests get no fallback; that is intended, but nothing in the response
  says which deployment actually served it.
