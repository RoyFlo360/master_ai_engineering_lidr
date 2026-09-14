from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env from the project root so settings load the same way regardless of
# the current working directory the server is started from.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8")

    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    DEEPSEEK_API_KEY: str | None = None
    LLM_PROVIDER: Literal["deepseek", "anthropic"] = "deepseek"
    LLM_MODEL: str = "deepseek-flash"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"

    @property
    def llm_api_key(self) -> str | None:
        """Credential for the configured provider.

        DeepSeek is consumed through the OpenAI-compatible API, so the key may be
        provided as DEEPSEEK_API_KEY or, for backwards compatibility, in the
        OPENAI_API_KEY slot.
        """
        if self.LLM_PROVIDER == "deepseek":
            return self.DEEPSEEK_API_KEY or self.OPENAI_API_KEY
        if self.LLM_PROVIDER == "anthropic":
            return self.ANTHROPIC_API_KEY
        return None

    @model_validator(mode="after")
    def validate_api_key_for_provider(self) -> "Settings":
        """Ensure the API key for the selected LLM provider is present."""
        if not self.llm_api_key:
            raise ValueError(
                f"No API key configured for LLM_PROVIDER={self.LLM_PROVIDER!r}. "
                "Set DEEPSEEK_API_KEY (or OPENAI_API_KEY) for 'deepseek', "
                "or ANTHROPIC_API_KEY for 'anthropic'."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings (singleton)."""
    return Settings()
