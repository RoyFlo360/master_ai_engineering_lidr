"""Unit tests for the wrapper's pure pieces: call kwargs, usage shape, pricing.

No Redis, no Router, no network — these call the methods directly with a stub.
"""

from types import SimpleNamespace

import pytest

from app.schemas.new_estimation_schema import EstimationResult
from app.services.llm_wrapper import (
    LLMWrapper,
    _estimate_cost,
    _usage_from_result,
    _usage_payload,
)


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


def test_usage_from_result_keeps_the_tokens_of_a_replay_but_not_its_cost():
    """The cached counts do not change between replays; the bill does."""
    result = {
        "usage": {"input_tokens": 2063, "output_tokens": 812, "total_tokens": 2875},
        "cost_usd": 0.000797,
        "cache_hit": True,
    }

    assert _usage_from_result(result) == {
        "input_tokens": 2063,
        "output_tokens": 812,
        "total_tokens": 2875,
        "cost_usd": 0.0,
    }


def test_usage_from_result_charges_a_cache_miss_what_the_call_cost():
    result = {
        "usage": {"input_tokens": 2063, "output_tokens": 812, "total_tokens": 2875},
        "cost_usd": 0.000797,
        "cache_hit": False,
    }

    assert _usage_from_result(result)["cost_usd"] == 0.000797


# --- structured chat: DeepSeek thinking must be off --------------------------


class RecordingInstructor:
    """Captures the kwargs Instructor would forward to litellm."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sentinel = object()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.sentinel


def build_chat_stub(*, enable_thinking: bool = False) -> LLMWrapper:
    """A real ``LLMWrapper`` without its ``__init__`` (no Router, no cache)."""
    stub = object.__new__(LLMWrapper)
    stub.primary_model = "deepseek/deepseek-flash"
    stub.openai_api_key = "openai-key"
    stub.deepseek_api_key = "deepseek-key"
    stub.anthropic_api_key = "anthropic-key"
    stub.timeout = 30
    stub.enable_thinking = enable_thinking
    stub._instructor = RecordingInstructor()
    return stub


def structured_chat(stub: LLMWrapper, model_override: str | None = None):
    return LLMWrapper.complete_structured_chat(
        stub,
        messages=[{"role": "user", "content": "hi"}],
        response_model=EstimationResult,
        model_override=model_override,
    )


def test_structured_chat_disables_deepseek_thinking_by_default():
    """Regression: Instructor sends ``tool_choice``, and DeepSeek rejects it with
    "Thinking mode does not support this tool_choice" unless thinking is off."""
    stub = build_chat_stub(enable_thinking=False)

    structured_chat(stub)

    assert stub._instructor.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_structured_chat_leaves_thinking_on_when_configured():
    stub = build_chat_stub(enable_thinking=True)

    structured_chat(stub)

    assert "extra_body" not in stub._instructor.calls[0]


def test_structured_chat_never_sends_the_deepseek_switch_to_anthropic():
    stub = build_chat_stub(enable_thinking=False)

    structured_chat(stub, model_override="claude-haiku-4-5-20251001")

    assert "extra_body" not in stub._instructor.calls[0]


def test_structured_chat_uses_the_deepseek_key_for_deepseek():
    stub = build_chat_stub()

    structured_chat(stub)

    assert stub._instructor.calls[0]["api_key"] == "deepseek-key"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("deepseek/deepseek-flash", "deepseek-key"),
        ("gpt-4o-mini", "openai-key"),
        ("claude-haiku-4-5-20251001", "anthropic-key"),
    ],
)
def test_api_key_for_routes_each_provider_to_its_own_key(model, expected):
    assert build_chat_stub()._api_key_for(model) == expected


def test_api_key_for_keeps_the_deepseek_slot_fallback():
    stub = build_chat_stub()
    stub.deepseek_api_key = None

    assert stub._api_key_for("deepseek/deepseek-flash") == "openai-key"


def test_the_metadata_extractor_targets_openai_with_the_openai_key():
    """Regression: the gpt-4o-mini fallback was sent the DeepSeek key, which
    OpenAI rejected as an incorrect API key."""
    stub = build_chat_stub()

    structured_chat(stub, model_override="gpt-4o-mini")

    assert stub._instructor.calls[0]["api_key"] == "openai-key"
    assert "extra_body" not in stub._instructor.calls[0]  # OpenAI, not DeepSeek


def test_instructor_forwards_the_thinking_switch_to_litellm():
    """Ends the chain: our kwargs reach ``litellm.completion`` through Instructor."""
    import instructor

    captured: dict = {}

    def recorder(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    stub = build_chat_stub(enable_thinking=False)
    stub._instructor = instructor.from_litellm(recorder)

    with pytest.raises(Exception, match="stop after capture"):
        LLMWrapper.complete_structured_chat(
            stub,
            messages=[{"role": "user", "content": "hi"}],
            response_model=EstimationResult,
            max_retries=0,
        )

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "tool_choice" in captured  # what DeepSeek rejects while thinking is on
