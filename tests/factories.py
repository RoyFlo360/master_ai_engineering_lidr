"""Test factories: hermetic settings and a fake OpenAI-compatible client.

``Settings`` is always built with ``_env_file=None`` so a developer's local
``.env`` (or a CI machine without one) cannot change a test outcome.
"""

from types import SimpleNamespace

from app.config import Settings

ESTIMATION_TEXT = (
    "## Inventory Platform\n\n"
    "| Task | Hours | Cost (EUR) |\n"
    "|------|------:|------------|\n"
    "| Discovery | 8 | 500 |"
)

DEFAULT_SETTINGS: dict = {
    "LLM_PROVIDER": "deepseek",
    "LLM_MODEL": "deepseek-flash",
    "DEEPSEEK_API_KEY": "test-deepseek-key",
    "OPENAI_API_KEY": None,
    "ANTHROPIC_API_KEY": None,
}


def build_settings(**overrides) -> Settings:
    """Return a Settings instance isolated from any .env file on disk."""
    return Settings(_env_file=None, **{**DEFAULT_SETTINGS, **overrides})


def make_completion(
    content: str = ESTIMATION_TEXT,
    finish_reason: str = "stop",
    *,
    model: str = "deepseek-flash",
    prompt_tokens: int = 2063,
    completion_tokens: int = 812,
    reasoning_tokens: int = 400,
) -> SimpleNamespace:
    """Build an object shaped like an OpenAI-compatible chat completion response."""
    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, reasoning_content="(chain of thought)"),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


class FakeLLM:
    """Stand-in for ``openai.OpenAI`` that records how the service drives it."""

    def __init__(self) -> None:
        self.client_kwargs: dict = {}
        self.requests: list[dict] = []
        self.response = make_completion()
        self.error: Exception | None = None
        self.chat = SimpleNamespace(completions=self)

    # -- test configuration ---------------------------------------------------
    def reply(
        self,
        content: str = ESTIMATION_TEXT,
        finish_reason: str = "stop",
        **usage,
    ) -> "FakeLLM":
        """Queue the response the next completion request will return."""
        self.response = make_completion(content, finish_reason, **usage)
        return self

    def fail_with(self, error: Exception) -> "FakeLLM":
        """Make the next completion request raise."""
        self.error = error
        return self

    # -- the surface used by the application ----------------------------------
    def __call__(self, **kwargs):  # OpenAI(api_key=..., base_url=...)
        self.client_kwargs = kwargs
        return self

    def create(self, **kwargs):  # client.chat.completions.create(...)
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response

    @property
    def last_request(self) -> dict:
        return self.requests[-1]
