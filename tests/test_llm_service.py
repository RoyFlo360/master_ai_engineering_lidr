"""Tests for the LLM service: request shape, response mapping and failure modes."""

import pytest

from app.services import llm_service
from app.services.llm_service import DEEPSEEK_BASE_URL, MAX_TOKENS, LLMServiceError
from tests.factories import ESTIMATION_TEXT, build_settings

TRANSCRIPTION = (
    "Client meeting: the client needs a ticket management platform with SSO login, "
    "SLA timers, email notifications and a reporting dashboard, delivered in three months."
)


def test_system_prompt_carries_reference_examples_and_the_required_format():
    prompt = llm_service.build_system_prompt()

    assert "--- EXAMPLE 1 ---" in prompt
    assert "Task, Hours, Cost (EUR)" in prompt
    assert "Recommended team composition" in prompt


def test_generate_estimation_calls_the_deepseek_endpoint(llm, settings):
    llm_service.generate_estimation(TRANSCRIPTION)

    assert llm.client_kwargs["base_url"] == DEEPSEEK_BASE_URL
    assert llm.client_kwargs["api_key"] == settings.llm_api_key == "test-deepseek-key"

    request = llm.last_request
    assert request["model"] == settings.LLM_MODEL
    assert request["max_tokens"] == MAX_TOKENS
    assert [message["role"] for message in request["messages"]] == ["system", "user"]
    assert request["messages"][1]["content"] == TRANSCRIPTION


def test_generate_estimation_returns_estimation_with_metadata_and_usage(llm):
    llm.reply(ESTIMATION_TEXT, "stop", prompt_tokens=2063, completion_tokens=812)

    result = llm_service.generate_estimation(TRANSCRIPTION)

    assert result["estimation"] == ESTIMATION_TEXT
    assert result["model"] == "deepseek-flash"
    assert result["provider"] == "deepseek"
    assert result["usage"] == {
        "input_tokens": 2063,
        "output_tokens": 812,
        "total_tokens": 2875,
    }


def test_generation_truncated_at_the_output_cap_raises_an_actionable_error(llm):
    """Regression: a starved generation used to come back as estimation=''."""
    llm.reply("", "length", completion_tokens=MAX_TOKENS, reasoning_tokens=MAX_TOKENS)

    with pytest.raises(LLMServiceError) as excinfo:
        llm_service.generate_estimation(TRANSCRIPTION)

    message = str(excinfo.value)
    assert str(MAX_TOKENS) in message
    assert "reasoning_tokens" in message


def test_empty_estimation_is_rejected(llm):
    llm.reply("   ", "stop")

    with pytest.raises(LLMServiceError, match="no usable estimation"):
        llm_service.generate_estimation(TRANSCRIPTION)


def test_aborted_generation_is_rejected(llm):
    llm.reply(ESTIMATION_TEXT, "aborted")

    with pytest.raises(LLMServiceError, match="no usable estimation"):
        llm_service.generate_estimation(TRANSCRIPTION)


def test_provider_errors_are_wrapped_with_the_original_cause(llm):
    llm.fail_with(RuntimeError("connection reset by peer"))

    with pytest.raises(LLMServiceError, match="connection reset by peer") as excinfo:
        llm_service.generate_estimation(TRANSCRIPTION)

    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_unsupported_provider_raises_instead_of_returning_an_empty_payload(
    settings_holder, llm
):
    """Regression: anything other than the hardcoded provider name returned {}."""
    settings_holder.current = build_settings(
        LLM_PROVIDER="anthropic", LLM_MODEL="claude-sonnet", ANTHROPIC_API_KEY="test-key"
    )

    with pytest.raises(LLMServiceError, match="Unsupported LLM provider"):
        llm_service.generate_estimation(TRANSCRIPTION)

    assert llm.requests == []


def test_output_cap_leaves_room_for_the_chain_of_thought():
    """Regression guard: the model bills its reasoning tokens against max_tokens."""
    assert MAX_TOKENS > 4000
