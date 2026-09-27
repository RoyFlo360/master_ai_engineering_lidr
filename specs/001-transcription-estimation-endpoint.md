# 001 — Transcription estimation endpoint

- **Status:** Implemented
- **Date:** backfilled 2026-09-27; describes code that predates this folder
- **Scope:** `app/services/llm_service.py`, `app/context/examples.py`,
  `app/schemas/estimation_schema.py`, `app/routers/estimations.py`,
  `streamlit_app_estimation_api.py`
- **Supersedes / Superseded by:** —

## Problem

The service had to turn a raw meeting transcription into a software project
estimation. That needed three things that are easy to get wrong: a prompt good enough
to produce a usable estimate, a response that tells the caller what the call cost, and
failure modes that a client can act on.

The failure modes are the interesting part. Provider errors are obvious; a *successful*
HTTP call carrying an empty estimation is not. `finish_reason="length"` with no
content, or a whitespace-only answer, used to come back as `200` with
`estimation: ""` — the client rendered a blank page and the cause (a starved output
cap, with the reasoning tokens billed against it) was nowhere in the response.

## Goals

- One call: transcription in, estimation plus `model`, `provider` and `usage` out.
- The prompt carries reference examples and states the required output format.
- Every way the generation can fail to produce an answer surfaces as an error the
  caller can read, never as an empty success.
- Token counts and cost are reported; they are never request fields.

## Non-goals

- Typed inputs (project type, detail level, output format) — that is
  [004](004-prompt-v1-estimation-endpoint.md).
- Streaming — that is [002](002-streaming-estimation-endpoint.md).
- Caching — that is [003](003-exact-match-redis-cache.md).
- Multiple providers. Only DeepSeek is wired; `LLM_PROVIDER=anthropic` is *accepted by
  the settings* and *rejected by the service* (see Follow-ups).

## Design

**The prompt** (`build_system_prompt`) is assembled in Python from
`app/context/examples.py`: a consultant role, three reference estimations rendered by
`format_examples_for_prompt` (as `--- EXAMPLE n ---` blocks), the required output shape
(H2 title, `Task | Hours | Cost (EUR)` table, totals, team composition, duration in
weeks) and the rates to price with (62.50 EUR/h developer, 50 EUR/h designer).

**The call** goes through the OpenAI SDK against DeepSeek's OpenAI-compatible endpoint
(`DEEPSEEK_BASE_URL`), with the credential taken from `settings.llm_api_key` — which
accepts `DEEPSEEK_API_KEY` or, for backwards compatibility, the `OPENAI_API_KEY` slot.

**`MAX_TOKENS = 8000` is not arbitrary.** `deepseek-flash` runs in thinking mode by
default and the chain of thought is billed as *output* tokens, so the cap has to cover
the reasoning budget plus the answer. The call asks for `reasoning_effort="low"` and
sends `extra_body={"thinking": {"type": "disabled"}}`; the cap is the belt to that
braces. A guard test asserts `MAX_TOKENS > 4000`.

**The guards, in order:**

```python
if finish_reason == "length":
    raise LLMServiceError(... MAX_TOKENS, output_tokens, reasoning_tokens ...)
if finish_reason != "stop" or not estimation:
    raise LLMServiceError("... returned no usable estimation ...")
```

The truncation message names the constant to raise and the numbers that explain it —
the reader of a 500 should not have to reproduce the call to find out why. The second
guard catches `aborted` and whitespace-only answers.

**Error mapping:** `LLMServiceError` propagates to the router, which answers `500` with
the message in `detail`. Anything else the provider raises is wrapped with the original
cause (`raise ... from exc`), so the traceback keeps the provider's error.

## Contract

```
POST /api/v1/estimate
{"transcription": "at least 50 characters of meeting transcription"}

→ 200
{
  "estimation": "## Inventory Platform\n\n| Task | Hours | …",
  "model": "deepseek-flash",
  "provider": "deepseek",
  "usage": {"input_tokens": 2063, "output_tokens": 812,
            "total_tokens": 2875, "cost_usd": 0.0008}
}
```

- `422` — `transcription` missing or shorter than 50 characters.
- `500` — the provider call failed, the generation was truncated at the cap, or the
  answer was unusable; `detail` carries the message.

## Verification

- `uv run pytest tests/test_llm_service.py tests/test_estimations_api.py -q` → 19 passed.
- `test_generate_estimation_calls_the_deepseek_endpoint` asserts the base URL, the
  credential, `max_tokens == MAX_TOKENS`, and the `system`/`user` message order.
- `test_generation_truncated_at_the_output_cap_raises_an_actionable_error` is the
  regression for the empty-success bug, and asserts the message contains both
  `MAX_TOKENS` and `reasoning_tokens`.
- `test_empty_estimation_is_rejected` and `test_aborted_generation_is_rejected` cover
  the second guard; `test_provider_errors_are_wrapped_with_the_original_cause` asserts
  `__cause__` survives.
- `test_unsupported_provider_raises_instead_of_returning_an_empty_payload` is a
  regression too: an unknown provider used to return `{}` with a 200. It also asserts
  no request was made.
- `test_estimate_returns_a_complete_estimation_response` and the two 422 tests go
  through the real app with `openai.OpenAI` replaced by `tests/factories.FakeLLM`.

Not verified: the guards against a *live* provider — tests stub the client, so the real
truncation behaviour is inferred from the provider's contract, not observed.

## Follow-ups

- **There are two pricing implementations.** `llm_service.cost_llm` has its own
  `PRICING` table (deepseek-flash only) and rounds to **4** decimals, so this endpoint
  reports `cost_usd: 0.0008`. `llm_wrapper._estimate_cost` has `MODEL_COSTS` (six
  models) and rounds to **6**, so the streaming and typed flows report `0.000797`. Two
  tables, two precisions, one product. Unifying them is the obvious follow-up; the
  6-decimal rounding is the one that survives (4 dp hides any call under ~$0.00005).
- **Two example sources.** `app/context/examples.py` serves this flow;
  `app/prompts/estimation/v1/examples.j2` serves 004. Retire one when this flow is
  retired.
- `MAX_TOKENS` here and `Settings.MAX_OUTPUT_TOKENS` both equal 8000 by coincidence,
  not by construction. Nothing keeps them in step.
- `LLM_PROVIDER=anthropic` passes settings validation and then fails every request with
  `Unsupported LLM provider`. Either implement it or reject it at startup.
