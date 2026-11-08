"""Unit tests for grounded generation and citation validation (T0B).

Two promises are pinned here, and they are the reason this chain exists:

* **No context, no estimate.** ``generate_estimate`` must not call the model at
  all when the context is empty, and must return ``grounded=False``,
  ``citations=[]``, ``confidence_pct=0``. A model answering from memory is the
  failure mode this whole layer was built to prevent, so the *call count* is
  asserted, not just the flags.
* **No invented citations.** ``check_citations`` must report an id that was not
  retrieved, and ``strip_unknown`` must remove exactly those citations, flipping
  ``grounded`` when none survive.

The wrapper is a local fake that answers by ``response_model``. No provider, no
network, no database.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.generation.rag.errors import GenerationError
from app.generation.rag.estimator import generate_estimate
from app.generation.rag.schemas import (
    Citation,
    EstimatedModule,
    EstimatedTask,
    EstimationQuery,
    GroundedEstimate,
)
from app.generation.rag.validation import check_citations, strip_unknown

CONTEXT = (
    "<source id=1 budget=BUD-2024-001 sector=finance year=2024>\n"
    "The authentication module took 120 hours across two sprints.\n"
    "<source id=2 budget=BUD-2024-002 sector=finance year=2024>\n"
    "Token rotation added 40 hours.\n"
)
CONTEXT_MARKER = "The authentication module took 120 hours"

USAGE = {
    "input_tokens": 300,
    "output_tokens": 120,
    "total_tokens": 420,
    "cost_usd": 0.0,
    "prompt_tokens": 300,
    "completion_tokens": 120,
}


class FakeWrapper:
    """Structured-call recorder; answers with a canned model, or raises."""

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

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self._answer(kwargs.get("response_model"))


def make_query() -> EstimationQuery:
    return EstimationQuery(
        summary="OAuth 2.0 authentication backend with RBAC.",
        search_query="oauth authentication backend rbac finance",
        sector="finance",
        technologies=["python"],
        project_year=2024,
        confidence=0.8,
        source="llm",
    )


def make_estimate(
    *,
    chunk_ids: list[int] | None = None,
    grounded: bool = True,
    confidence_pct: int = 74,
) -> GroundedEstimate:
    ids = [1, 2] if chunk_ids is None else chunk_ids
    return GroundedEstimate(
        summary="Authentication backend, 160 hours.",
        modules=[
            EstimatedModule(
                name="Authentication",
                tasks=[EstimatedTask(name="OAuth 2.0 flow", estimated_hours=160)],
                estimated_hours=160,
            )
        ],
        total_hours=160,
        citations=[
            Citation(
                chunk_id=chunk_id,
                budget_id=f"BUD-2024-{chunk_id:03d}",
                claim=f"Supports claim {chunk_id}.",
            )
            for chunk_id in ids
        ],
        confidence_pct=confidence_pct,
        grounded=grounded,
    )


def run(coro):
    return asyncio.run(coro)


# --- check_citations / strip_unknown ----------------------------------------


def test_check_citations_accepts_a_fully_retrieved_estimate() -> None:
    check = check_citations(make_estimate(), {1, 2, 3})

    assert check.ok is True
    assert check.unknown_ids == []


def test_check_citations_reports_the_unknown_id() -> None:
    estimate = make_estimate(chunk_ids=[1, 999])

    check = check_citations(estimate, {1, 2})

    assert check.ok is False
    # The offending id is named, so the orchestrator can put it in the retry prompt.
    assert check.unknown_ids == [999]
    assert "999" in check.message


def test_strip_unknown_removes_only_the_bad_citations() -> None:
    estimate = make_estimate(chunk_ids=[1, 999, 2])

    cleaned = strip_unknown(estimate, {1, 2})

    assert isinstance(cleaned, GroundedEstimate)
    assert [citation.chunk_id for citation in cleaned.citations] == [1, 2]
    assert cleaned.grounded is True
    # Only ``cleaned`` is asserted: the contract fixes the return value, and
    # whether the helper copies or rebuilds is an implementation choice.


def test_strip_unknown_flips_grounded_when_no_citation_survives() -> None:
    estimate = make_estimate(chunk_ids=[999])

    cleaned = strip_unknown(estimate, {1, 2})

    assert cleaned.citations == []
    assert cleaned.grounded is False


# --- generate_estimate ------------------------------------------------------


def test_a_grounded_estimate_is_passed_through() -> None:
    expected = make_estimate()
    wrapper = FakeWrapper(expected)

    result = run(
        generate_estimate(make_query(), CONTEXT, source_ids={1, 2}, wrapper=wrapper)
    )

    assert isinstance(result, GroundedEstimate)
    assert result.grounded is True
    assert result.summary == expected.summary
    assert result.total_hours == expected.total_hours
    assert [citation.chunk_id for citation in result.citations] == [1, 2]
    assert result.confidence_pct == 74
    assert len(wrapper.calls) == 1


def test_the_query_and_the_context_reach_the_model() -> None:
    wrapper = FakeWrapper(make_estimate())

    run(generate_estimate(make_query(), CONTEXT, source_ids={1, 2}, wrapper=wrapper))

    prompt = repr(wrapper.calls[0])
    assert CONTEXT_MARKER in prompt
    assert "oauth authentication backend rbac finance" in prompt


def test_empty_context_never_calls_the_model() -> None:
    wrapper = FakeWrapper(make_estimate())

    result = run(generate_estimate(make_query(), "", source_ids={1, 2}, wrapper=wrapper))

    assert result.grounded is False
    assert result.citations == []
    assert result.confidence_pct == 0
    # The point of the branch: no billed call, nothing answered from memory.
    assert wrapper.calls == []


def test_empty_context_with_no_source_ids_is_still_ungrounded() -> None:
    wrapper = FakeWrapper(make_estimate())

    result = run(generate_estimate(make_query(), "", source_ids=set(), wrapper=wrapper))

    assert result.grounded is False
    assert result.confidence_pct == 0
    assert wrapper.calls == []


def test_a_failing_wrapper_is_a_generation_error() -> None:
    secret = "credential-material-alpha"
    wrapper = FakeWrapper(error=RuntimeError(f"upstream 502 api_key={secret}"))

    with pytest.raises(GenerationError) as raised:
        run(
            generate_estimate(
                make_query(), CONTEXT, source_ids={1, 2}, wrapper=wrapper
            )
        )

    # The public message is what a client sees; the provider detail stays out.
    assert secret not in raised.value.public_message
