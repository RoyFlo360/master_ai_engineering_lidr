from structlog import get_logger

from app.config import get_settings
from app.context.examples import ESTIMATION_EXAMPLES, format_examples_for_prompt

log = get_logger()

# deepseek-flash runs in thinking mode by default ("high" effort), and the
# chain-of-thought is billed as output tokens, so the cap has to cover the
# reasoning budget *plus* the estimation itself. Too small a cap means generation
# stops with finish_reason="length" before any answer is written.
MAX_TOKENS = 8000

"""DeepSeek exposes an OpenAI-compatible API, so the OpenAI SDK is reused with
only the base URL and credential swapped. """
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class LLMServiceError(Exception):
    """Raised when the LLM provider call fails."""


def build_system_prompt() -> str:
    """Construct the system prompt with role definition and reference examples."""
    examples_text = format_examples_for_prompt(ESTIMATION_EXAMPLES)
    return (
        "You are a senior software consultant with 15+ years of experience in project "
        "estimation. Your task is to produce a detailed software project estimation based "
        "on a meeting transcription provided by the user.\n\n"
        "Below are reference estimations from previous projects. Use them as a guide for "
        "structure, level of detail, and realistic pricing. Adapt the content to match the "
        "specific project described in the transcription.\n\n"
        "Your output MUST follow this exact format:\n"
        "- Project title as an H2 heading\n"
        "- A task breakdown table with columns: Task, Hours, Cost (EUR)\n"
        "- Total hours\n"
        "- Total cost in EUR\n"
        "- Recommended team composition\n"
        "- Estimated duration in weeks\n\n"
        "Use a developer rate of approximately 62.50 EUR/hour (500 EUR/day) and a designer "
        "rate of approximately 50 EUR/hour (400 EUR/day). Provide realistic, well-justified "
        "numbers.\n\n"
        f"{examples_text}"
    )


def generate_estimation(transcription: str) -> dict:
    """Generate a software estimation from a meeting transcription using the configured LLM."""
    settings = get_settings()
    system_prompt = build_system_prompt()

    log.info("generating_estimation", provider=settings.LLM_PROVIDER, model=settings.LLM_MODEL)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": transcription},
    ]

    try:
        if settings.LLM_PROVIDER == "deepseek":
            return _call_deepseek(messages=messages)
        raise LLMServiceError(f"Unsupported LLM provider: {settings.LLM_PROVIDER!r}")
    except LLMServiceError:
        raise
    except Exception as exc:
        log.error("llm_call_failed", error=str(exc), provider=settings.LLM_PROVIDER)
        raise LLMServiceError(f"LLM call failed: {exc}") from exc


def cost_llm(input_tokens:float, output_tokens:float, llm_model) -> float:
    PRICING = {
        "deepseek-flash": {"input": 0.15, "output": 0.60},
    }
    prices = PRICING.get(llm_model, {"input": 0, "output": 0})
    cost = (
            (input_tokens / 1_000_000) * prices["input"] +
            (output_tokens / 1_000_000) * prices["output"]
    )

    return round(cost, 4)


def _call_deepseek(messages: list[dict]) -> dict:
    """Send a chat completion request to DeepSeek's OpenAI-compatible API."""
    from openai import OpenAI

    settings = get_settings()
    client = OpenAI(api_key=settings.llm_api_key, base_url=DEEPSEEK_BASE_URL)

    response = client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=messages,
        max_tokens=MAX_TOKENS,
        reasoning_effort="low", # low | high | max
        extra_body={"thinking": {"type": "disabled"}}
        #max_output_tokens=800,
        #reasoning={"effort": "medium"},  # ✅ New: controls reasoning depth
        #text={"verbosity": "low"},  # ✅ New: controls answer length
    )

    choice = response.choices[0]
    usage = response.usage
    finish_reason = choice.finish_reason
    reasoning_tokens = getattr(
        getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
    )

    log.info(
        "llm_response_received",
        provider=settings.LLM_PROVIDER,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        reasoning_tokens=reasoning_tokens,
        finish_reason=finish_reason,
    )

    estimation = (choice.message.content or "").strip()

    if finish_reason == "length":
        raise LLMServiceError(
            f"DeepSeek stopped at the {MAX_TOKENS}-token output cap before writing the "
            f"estimation (output_tokens={usage.completion_tokens}, "
            f"reasoning_tokens={reasoning_tokens}). Raise MAX_TOKENS in "
            "app/services/llm_service.py."
        )
    if finish_reason != "stop" or not estimation:
        raise LLMServiceError(
            "DeepSeek returned no usable estimation "
            f"(finish_reason={finish_reason!r}, output_tokens={usage.completion_tokens}, "
            f"reasoning_tokens={reasoning_tokens})."
        )

    return {
        "estimation": estimation,
        "model": response.model,
        "provider": settings.LLM_PROVIDER,
        "usage": {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": cost_llm(usage.prompt_tokens, usage.completion_tokens, settings.LLM_MODEL),
        },
    }
