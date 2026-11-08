"""Unit tests for the transcript -> ``EstimationQuery`` reformulator (T0B).

The reformulator is the only stage that *interprets* free text, so it is also the
stage most likely to fail in production (provider outage, bad JSON, a prompt
that stops conforming). The contract is therefore two-sided:

* the happy path returns whatever the LLM produced, unchanged and bounded;
* **every** failure degrades to ``source="fallback"`` with a searchable string
  and a confidence low enough that downstream cannot present it as a confident
  estimate. An exception escaping this stage would take the whole chain down for
  a recoverable provider problem.

Hermetic: the wrapper is a local fake. No provider, no network, no ``Settings``
read. The canned model answers by ``response_model`` (``rules/testing.md``), so
the fake also survives a private envelope type.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.generation.rag.errors import ReformulationError
from app.generation.rag.query_reformulator import reformulate
from app.generation.rag.schemas import MAX_SEARCH_QUERY_CHARS, EstimationQuery

#: A realistic transcript; > MIN_TRANSCRIPT_CHARS and carries the distinctive
#: token ``OAUTHMARKER`` that a fallback query must preserve.
TRANSCRIPT = (
    "We need an estimation for the OAUTHMARKER authentication backend: OAuth 2.0 "
    "authorization code flow with PKCE, refresh-token rotation, RBAC for three "
    "roles and an admin console. The client is a mid-size insurer; we expect "
    "Python and PostgreSQL, delivery during 2024."
)

#: A credential-shaped sentinel. Never written as a provider key literal.
SECRET = "credential-material-alpha"

USAGE = {
    "input_tokens": 120,
    "output_tokens": 40,
    "total_tokens": 160,
    "cost_usd": 0.0,
    "prompt_tokens": 120,
    "completion_tokens": 40,
}


class FakeWrapper:
    """Records structured calls and answers with a canned model instance.

    ``rules/testing.md``: a structured fake answers by ``response_model``. If the
    canned value is not an instance of the requested model (an internal envelope
    around ``EstimationQuery``), the fake coerces it rather than handing the
    pipeline a dict where a model is expected.
    """

    def __init__(self, result: object | None = None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def _answer(self, response_model):
        if self.error is not None:
            raise self.error
        result = self.result
        if response_model is None or not isinstance(response_model, type):
            return result, dict(USAGE)
        if isinstance(result, response_model):
            return result, dict(USAGE)
        try:
            return response_model.model_validate(result.model_dump()), dict(USAGE)
        except ValidationError:
            pass
        for name, field in response_model.model_fields.items():
            annotation = field.annotation
            if isinstance(annotation, type) and isinstance(result, annotation):
                return response_model(**{name: result}), dict(USAGE)
        raise AssertionError(
            f"fake cannot answer response_model={response_model!r} with {result!r}"
        )

    def complete_structured_chat(self, **kwargs):
        self.calls.append(kwargs)
        return self._answer(kwargs.get("response_model"))

    #: Some call sites use the non-chat structured variant; accept either.
    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self._answer(kwargs.get("response_model"))


def canned_query(**overrides) -> EstimationQuery:
    payload = {
        "summary": "OAuth 2.0 authentication backend with RBAC for an insurer.",
        "search_query": "oauth 2.0 authentication backend rbac insurance 2024",
        "sector": "insurance",
        "technologies": ["python", "postgresql"],
        "project_year": 2024,
        "confidence": 0.82,
        "source": "llm",
    }
    payload.update(overrides)
    return EstimationQuery(**payload)


def run(coro):
    return asyncio.run(coro)


# --- the happy path ---------------------------------------------------------


def test_the_llm_query_is_passed_through_unchanged() -> None:
    expected = canned_query()
    wrapper = FakeWrapper(expected)

    result = run(reformulate(TRANSCRIPT, wrapper=wrapper))

    assert isinstance(result, EstimationQuery)
    assert result.summary == expected.summary
    assert result.search_query == expected.search_query
    assert result.sector == "insurance"
    assert result.technologies == ["python", "postgresql"]
    assert result.project_year == 2024
    assert result.confidence == pytest.approx(0.82)
    assert result.source == "llm"


def test_the_llm_is_called_exactly_once() -> None:
    wrapper = FakeWrapper(canned_query())

    run(reformulate(TRANSCRIPT, wrapper=wrapper))

    assert len(wrapper.calls) == 1


def test_the_transcript_reaches_the_model() -> None:
    wrapper = FakeWrapper(canned_query())

    run(reformulate(TRANSCRIPT, wrapper=wrapper))

    assert "OAUTHMARKER" in repr(wrapper.calls[0])


def test_the_model_override_is_forwarded() -> None:
    wrapper = FakeWrapper(canned_query())

    run(reformulate(TRANSCRIPT, wrapper=wrapper, model="deepseek-reasoner"))

    call = wrapper.calls[0]
    recorded = [
        call.get("model_override"),
        call.get("model"),
        call.get("model_name"),
    ]
    assert "deepseek-reasoner" in recorded


def test_an_over_long_search_query_is_truncated_not_rejected() -> None:
    long_query = "authentication backend " * 400  # ~9.6k chars, far past the cap
    assert len(long_query) > MAX_SEARCH_QUERY_CHARS
    wrapper = FakeWrapper(canned_query())
    # Bypass the schema bound to simulate a model that ignored the instruction:
    # the reformulator must clamp, not blow up on a ValidationError.
    wrapper.result = canned_query().model_copy(update={"search_query": long_query})

    result = run(reformulate(TRANSCRIPT, wrapper=wrapper))

    assert result.search_query  # never empty
    assert len(result.search_query) <= MAX_SEARCH_QUERY_CHARS
    # Truncation may snap to a word boundary; a substring is the correct claim.
    assert result.search_query in long_query
    assert result.source == "llm"


# --- degradation ------------------------------------------------------------


def test_a_failing_wrapper_degrades_to_the_fallback_instead_of_raising() -> None:
    wrapper = FakeWrapper(error=RuntimeError("provider exploded after 3 retries"))

    result = run(reformulate(TRANSCRIPT, wrapper=wrapper))

    assert result.source == "fallback"
    assert 0.0 <= result.confidence <= 0.3
    assert result.search_query.strip()
    assert len(result.search_query) <= MAX_SEARCH_QUERY_CHARS
    assert result.summary.strip()


def test_the_fallback_query_keeps_the_transcript_content() -> None:
    wrapper = FakeWrapper(error=RuntimeError("rate limited"))

    result = run(reformulate(TRANSCRIPT, wrapper=wrapper))

    assert "OAUTHMARKER" in result.search_query


def test_a_failure_does_not_leak_provider_detail_into_the_query() -> None:
    wrapper = FakeWrapper(error=RuntimeError(f"upstream 401 api_key={SECRET}"))

    result = run(reformulate(TRANSCRIPT, wrapper=wrapper))

    assert SECRET not in result.search_query
    assert SECRET not in result.summary


@pytest.mark.parametrize(
    "transcript",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\t  ", id="whitespace-only"),
    ],
)
def test_an_empty_transcript_is_a_reformulation_error(transcript: str) -> None:
    wrapper = FakeWrapper(canned_query())

    with pytest.raises(ReformulationError):
        run(reformulate(transcript, wrapper=wrapper))

    # There is nothing to reformulate, so nothing is billed.
    assert wrapper.calls == []
