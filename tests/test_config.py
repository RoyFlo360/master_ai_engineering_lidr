"""Tests for settings loading and provider credential resolution."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ENV_FILE, Settings
from tests.factories import build_settings


def test_env_file_is_resolved_from_the_project_root():
    """Regression: env_file was '../.env', which resolves against the CWD.

    Started from the project root that path pointed outside the project, so the
    real .env was silently ignored and settings fell back to class defaults.
    """
    project_root = Path(__file__).resolve().parent.parent

    assert ENV_FILE.is_absolute()
    assert ENV_FILE == project_root / ".env"
    assert Settings.model_config["env_file"] == ENV_FILE


def test_settings_are_loaded_from_environment_variables(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key-from-environment")

    settings = Settings(_env_file=None)

    assert settings.LLM_PROVIDER == "deepseek"
    assert settings.LLM_MODEL == "deepseek-v4-pro"
    assert settings.llm_api_key == "key-from-environment"


def test_defaults_target_the_deepseek_provider(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)

    settings = Settings(_env_file=None, DEEPSEEK_API_KEY="test-key")

    assert settings.LLM_PROVIDER == "deepseek"
    assert settings.LLM_MODEL == "deepseek-flash"


def test_deepseek_key_takes_precedence_over_the_legacy_openai_slot():
    settings = build_settings(DEEPSEEK_API_KEY="dedicated-key", OPENAI_API_KEY="legacy-key")

    assert settings.llm_api_key == "dedicated-key"


def test_legacy_openai_slot_is_still_accepted_for_deepseek():
    """DeepSeek is consumed through the OpenAI-compatible API."""
    settings = build_settings(DEEPSEEK_API_KEY=None, OPENAI_API_KEY="legacy-key")

    assert settings.llm_api_key == "legacy-key"


def test_anthropic_provider_uses_the_anthropic_key():
    settings = build_settings(
        LLM_PROVIDER="anthropic", LLM_MODEL="claude-sonnet", ANTHROPIC_API_KEY="anthropic-key"
    )

    assert settings.llm_api_key == "anthropic-key"


def test_provider_without_any_key_is_rejected():
    with pytest.raises(ValidationError, match="No API key configured"):
        build_settings(DEEPSEEK_API_KEY=None, OPENAI_API_KEY=None)


def test_anthropic_without_a_key_is_rejected():
    with pytest.raises(ValidationError, match="No API key configured"):
        build_settings(LLM_PROVIDER="anthropic")


def test_unknown_provider_is_rejected():
    """LLM_PROVIDER only accepts providers the service can actually call."""
    with pytest.raises(ValidationError):
        build_settings(LLM_PROVIDER="openai", LLM_MODEL="gpt-4o-mini")
