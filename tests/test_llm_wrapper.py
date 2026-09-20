"""Unit tests for the wrapper's pure pieces: call kwargs, usage shape, pricing.

No Redis, no Router, no network — these call the methods directly with a stub.
"""

from types import SimpleNamespace

from app.services.llm_wrapper import LLMWrapper, _estimate_cost, _usage_payload


class StubWrapper:
    """Just the attributes ``_build_call_kwargs`` reads."""

    def __init__(self, *, enable_thinking: bool) -> None:
        self.enable_thinking = enable_thinking
        self.primary_model = "deepseek/deepseek-flash"


def build_kwargs(enable_thinking: bool = False, **overrides) -> dict:
    args = {
        "messages": [],
        "max_tokens": 8000,
        "thinking_budget": None,
        "model_override": None,
        "stream": True,
    } | overrides
    return LLMWrapper._build_call_kwargs(StubWrapper(enable_thinking=enable_thinking), **args)


def test_streaming_asks_the_provider_to_report_usage():
    kwargs = build_kwargs()

    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}


def test_thinking_is_off_by_default_and_the_switch_turns_it_back_on():
    assert build_kwargs()["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "extra_body" not in build_kwargs(enable_thinking=True)


def test_the_disable_switch_is_never_sent_to_a_provider_that_cannot_take_it():
    kwargs = build_kwargs(model_override="claude-haiku-4-5-20251001")

    assert "extra_body" not in kwargs


def test_an_explicit_thinking_budget_wins_over_the_off_switch():
    kwargs = build_kwargs(thinking_budget=2048)

    assert "extra_body" not in kwargs


def test_usage_payload_keeps_cost_inside_usage():
    usage = SimpleNamespace(prompt_tokens=2063, completion_tokens=812, total_tokens=2875)

    payload = _usage_payload("deepseek-flash", usage)

    assert payload == {
        "input_tokens": 2063,
        "output_tokens": 812,
        "total_tokens": 2875,
        "cost_usd": _estimate_cost("deepseek-flash", 2063, 812),
    }


def test_a_cache_hit_reports_zero_and_a_unknown_model_is_never_guessed_at():
    assert _usage_payload("deepseek-flash", None) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }
    assert _estimate_cost("some-unlisted-model", 1000, 1000) == 0.0
