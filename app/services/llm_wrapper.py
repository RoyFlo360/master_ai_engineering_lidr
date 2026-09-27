"""LiteLLM-backed wrapper that adds provider fallback, exact-match cache, cost tracking,
and structured logging to every LLM call in the estimator.

Design notes
------------
- ``complete_stream()`` yields SSE-ready dicts (``token`` … ``usage``) so the router
  forwards them unchanged and the event vocabulary lives in exactly one place. The
  blocking ``complete()`` path was removed as dead code: ``/estimate`` still goes
  through ``llm_service.py``, and reviving it there is the pending unification step.
- The Router is configured with two deployments under the same ``model_name``
  ("estimator") so LiteLLM can switch from primary to fallback transparently.
  When the caller overrides the model per-request (Session 2 live demos), we
  bypass the Router and call ``litellm.completion`` directly with explicit
  credentials — that path has no fallback by design.
- The cache key includes the full system prompt and the generation knobs, so any
  Session 2 toggle (preprocessing, num_examples, ACTIVE_OUTPUT_PROMPT) implicitly
  invalidates the cache without manual flushing.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from structlog import get_logger
from litellm import Router, completion

from app.services.cache import EstimationCache

log = get_logger()


# Cost per 1M tokens (USD). Update as pricing changes.
MODEL_COSTS: dict[str, dict[str, float]] = {
    "deepseek-flash": {"input": 0.15, "output": 0.60},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
}


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    base = _normalise_model_name(model)
    costs = MODEL_COSTS.get(base) or MODEL_COSTS.get(model)
    if costs is None:
        # ponytail: an unknown model is priced at zero rather than guessed at. Known
        # ceiling: cost reads $0.00 for any model missing from MODEL_COSTS — the
        # upgrade path is adding the entry there.
        log.warning("model_not_in_cost_table", model=model)
        costs = {"input": 0.0, "output": 0.0}
    return round((tokens_in * costs["input"] + tokens_out * costs["output"]) / 1_000_000, 6)


def _normalise_model_name(model: str) -> str:
    """Strip provider prefixes like ``anthropic/`` that LiteLLM may emit."""
    return model.split("/", 1)[1] if "/" in model else model


def _provider_from_model(model: str) -> str:
    name = _normalise_model_name(model).lower()
    if name.startswith("claude"):
        return "anthropic"
    elif name.startswith("gpt") or name.startswith("o1") or name.startswith("o3"):
        return "openai"
    elif name.startswith("deep"):
        return "deepseek"
    return "unknown"


class LLMWrapper:
    """Unified LLM client with cache, fallback, and cost tracking."""

    def __init__(
        self,
        *,
        openai_api_key: str | None,
        deepseek_api_key: str | None,
        anthropic_api_key: str | None,
        primary_model: str,
        fallback_model: str,
        timeout: int,
        num_retries: int,
        max_tokens: int,
        enable_thinking: bool,
        cache: EstimationCache,
    ):
        self.openai_api_key = openai_api_key
        self.deepseek_api_key = deepseek_api_key
        self.anthropic_api_key = anthropic_api_key
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.timeout = timeout
        self.num_retries = num_retries
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self.cache = cache

        self.router = Router(
            model_list=[
                {
                    "model_name": "estimator",
                    "litellm_params": {
                        "model": primary_model,
                        "api_key": deepseek_api_key,
                        "timeout": timeout,
                    },
                },
                {
                    "model_name": "estimator",
                    "litellm_params": {
                        "model": fallback_model,
                        "api_key": anthropic_api_key,
                        "timeout": timeout,
                    },
                },
            ],
            fallbacks=[{"estimator": ["estimator"]}],
            num_retries=num_retries,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------


    def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model_override: str | None = None,
        max_tokens: int = 4000,
        thinking_budget: int | None = None,
    ) -> dict[str, Any]:
        """Single LLM call with cache + (optional) fallback. Returns the legacy dict shape
        plus ``cache_hit`` and ``cost_usd`` fields.
        """
        cache_key_model = model_override or self.primary_model
        cache_key = EstimationCache.make_key(
            system_prompt=system_prompt,
            user_message=user_message,
            model=cache_key_model,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
        )
        cached = self.cache.get(cache_key)
        if cached:
            return {**cached, "cache_hit": True}

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        kwargs = self._build_call_kwargs(
            messages=messages,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
            model_override=model_override,
        )

        log.info(
            "llm_call_started",
            mode="blocking",
            model=model_override or self.primary_model,
            has_thinking=thinking_budget is not None,
        )
        t0 = time.perf_counter()
        try:
            response = self._dispatch(model_override=model_override, **kwargs)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log.error(
                "llm_call_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                latency_ms=latency_ms,
            )
            raise

        latency_ms = int((time.perf_counter() - t0) * 1000)
        result = self._normalise_response(response, latency_ms=latency_ms)
        log.info(
            "llm_call_completed",
            model=result["model"],
            provider=result["provider"],
            input_tokens=result["usage"]["input_tokens"],
            output_tokens=result["usage"]["output_tokens"],
            cost_usd=result["cost_usd"],
            latency_ms=latency_ms,
            finish_reason=result["finish_reason"],
        )
        self.cache.set(cache_key, result)
        return {**result, "cache_hit": False}


    @staticmethod
    def _normalise_response(response: Any, *, latency_ms: int) -> dict[str, Any]:
        choice = response.choices[0]
        finish_reason = (choice.finish_reason or "stop").lower()
        usage = response.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens) or (input_tokens + output_tokens)

        model = _normalise_model_name(response.model)
        return {
            "estimation": choice.message.content or "",
            "model": model,
            "provider": _provider_from_model(model),
            "finish_reason": finish_reason,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "latency_ms": latency_ms,
            "cost_usd": _estimate_cost(model, input_tokens, output_tokens),
        }


    def complete_stream(
        self,
        *,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        model_override: str | None = None,
    ) -> Iterator[dict]:
        """Yield SSE-ready events as the model streams: ``token`` … then ``usage``.

        The caller forwards them unchanged, so the event vocabulary lives here. A
        cache hit replays the cached text and reports zero usage, because nothing
        was billed for it.
        """
        model = model_override or self.primary_model
        cache_key = EstimationCache.make_key(
            system_prompt=system_prompt,
            user_message=user_message,
            model=model,
            max_tokens=max_tokens,
            thinking_budget=None,
        )
        cached = self.cache.get(cache_key)
        if cached:
            log.info("stream_cache_hit", chars=len(cached.get("estimation", "")))
            yield {"event": "token", "data": cached.get("estimation", "")}
            yield _usage_event(model, None)
            return

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        kwargs = self._build_call_kwargs(
            messages=messages,
            max_tokens=max_tokens,
            thinking_budget=None,
            model_override=model_override,
            stream=True,
        )

        log.info("llm_stream_started", model=model)
        t0 = time.perf_counter()
        full_text: list[str] = []
        usage = None
        try:
            response = self._dispatch(model_override=model_override, **kwargs)
            for chunk in response:
                # With stream_options.include_usage the provider sends one final
                # chunk that carries usage and no choices.
                usage = getattr(chunk, "usage", None) or usage
                delta = _extract_delta(chunk)
                if delta:
                    full_text.append(delta)
                    yield {"event": "token", "data": delta}
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log.error(
                "llm_stream_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                latency_ms=latency_ms,
            )
            raise

        latency_ms = int((time.perf_counter() - t0) * 1000)
        rendered = "".join(full_text)
        payload = _usage_payload(model, usage)
        log.info(
            "llm_stream_completed",
            latency_ms=latency_ms,
            chars=len(rendered),
            **payload,
        )
        yield {"event": "usage", "data": json.dumps(payload)}

        self.cache.set(
            cache_key,
            {
                "estimation": rendered,
                "model": model,
                "provider": _provider_from_model(model),
                "finish_reason": "stop",
                "usage": payload,
                "latency_ms": latency_ms,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_call_kwargs(
        self,
        *,
        messages: list[dict],
        max_tokens: int,
        thinking_budget: int | None,
        model_override: str | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
        }
        target_model = model_override or self.primary_model
        if thinking_budget is None and not self.enable_thinking:
            # DeepSeek thinks by default and bills the chain of thought as output, so
            # it has to be switched off explicitly. Anthropic's extended thinking is
            # opt-in, so there is nothing to disable there — and sending DeepSeek's
            # switch to it would be an invalid body on the fallback path.
            if _provider_from_model(target_model) == "deepseek":
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        if stream:
            kwargs["stream"] = True
            # OpenAI-compatible providers then send one last chunk carrying usage.
            kwargs["stream_options"] = {"include_usage": True}

        if thinking_budget is not None:
            if _provider_from_model(target_model) == "anthropic":
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
                kwargs["max_tokens"] = max(max_tokens, thinking_budget + 1024)
            else:
                log.warning(
                    "thinking_budget_ignored_for_provider",
                    provider=_provider_from_model(target_model),
                    model=target_model,
                )
        return kwargs

    def _dispatch(self, *, model_override: str | None, **kwargs: Any) -> Any:
        """Call the Router (with fallback) or LiteLLM directly when the caller
        wants a specific model."""
        if model_override:
            api_key = (
                self.anthropic_api_key
                if _provider_from_model(model_override) == "anthropic"
                else self.deepseek_api_key
            )
            return completion(
                model=model_override,
                api_key=api_key,
                timeout=self.timeout,
                num_retries=self.num_retries,
                **kwargs,
            )
        return self.router.completion(model="estimator", **kwargs)

def _usage_payload(model: str, usage: Any | None) -> dict[str, Any]:
    """Token counts and cost, in the one shape the API, cache and sidebar share.

    ``usage`` is the provider's usage object, or None for a cache hit — nothing was
    billed for a replay, so it reports zero. ponytail: zero-for-cache-hit is the
    actual-spend semantic; it deliberately understates what a cold run costs.
    """
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or (input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": _estimate_cost(model, input_tokens, output_tokens),
    }


def _usage_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Usage for one finished blocking call, in the shape :func:`_usage_payload` returns.

    A cache hit replays a stored answer: the token counts come from the cached
    entry, because an identical prompt tokenises identically and they do not
    change between replays, while ``cost_usd`` is zero — nothing was billed for
    the replay.
    """
    usage = result.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": 0.0 if result.get("cache_hit") else result.get("cost_usd", 0.0),
    }


def _usage_event(model: str, usage: Any | None) -> dict[str, str]:
    """The SSE frame that carries :func:`_usage_payload` to the client."""
    return {"event": "usage", "data": json.dumps(_usage_payload(model, usage))}


def _extract_delta(chunk: Any) -> str:
    """Pull the text delta out of a LiteLLM streaming chunk."""
    try:
        delta = chunk.choices[0].delta
    except (AttributeError, IndexError):
        return ""
    content = getattr(delta, "content", None)
    return content or ""