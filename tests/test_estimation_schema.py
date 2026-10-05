"""Unit tests for the typed estimation schemas and their business rules.

Two schemas answer two different questions, and the split is load-bearing:

- ``EstimationResponse`` is the free-text answer of ``POST /api/v1/new-estimate``
  and reports the call's billing.
- ``StructuredEstimationResponse`` carries an ``EstimationResult`` validated by
  Instructor, for the conversational and Session-4 service pipelines.

Adding a ``result`` field to the free-text schema (a Session-5 slip) broke every
``/new-estimate`` response and its 14 route tests; the first test below pins the
free-text shape so it cannot happen again.
"""

import pytest
from pydantic import ValidationError

from app.schemas.estimation_schema import TokenUsage
from app.schemas.new_estimation_schema import (
    EstimationResponse,
    EstimationResult,
    Phase,
    StructuredEstimationResponse,
)


def phase(**overrides) -> Phase:
    base = {
        "name": "Discovery",
        "duration_weeks": 1,
        "cost_eur": 500,
        "summary": "Discovery workshops and scope definition.",
    }
    return Phase(**{**base, **overrides})


def result(**overrides) -> EstimationResult:
    base = {
        "summary": "A short but sufficiently long overall summary.",
        "confidence_pct": 80,
        "phases": [phase()],
        "total_duration_weeks": 1,
        "total_cost_eur": 500,
    }
    return EstimationResult(**{**base, **overrides})


# --- the free-text contract stays free text ---------------------------------


def test_estimation_response_has_exactly_the_v1_fields():
    assert set(EstimationResponse.model_fields) == {
        "text",
        "prompt_version",
        "cache_hit",
        "usage",
    }


def test_estimation_response_ignores_an_unexpected_result_field():
    """Pydantic ignores extras by default, so a stray ``result`` is dropped,
    not echoed into the body."""
    response = EstimationResponse(
        text="an estimation",
        prompt_version="v1",
        cache_hit=False,
        usage=TokenUsage(input_tokens=1, output_tokens=2, total_tokens=3, cost_usd=0.0),
        result=result(),  # type: ignore[call-arg]
    )

    assert not hasattr(response, "result")
    assert "result" not in response.model_dump()


# --- the structured response carries a validated result ---------------------


def test_structured_response_carries_the_validated_result():
    estimation = result()

    response = StructuredEstimationResponse(
        result=estimation, prompt_version="v2", cache_hit=False
    )

    assert response.result is estimation
    assert response.result.total_cost_eur == 500
    assert set(StructuredEstimationResponse.model_fields) == {
        "result",
        "prompt_version",
        "cache_hit",
    }


# --- EstimationResult business rules ----------------------------------------


def test_phases_must_sum_to_the_total():
    with pytest.raises(ValidationError, match="does not match total_cost_eur"):
        result(total_cost_eur=900)


def test_a_phase_sum_mismatch_names_both_numbers():
    with pytest.raises(ValidationError) as excinfo:
        result(phases=[phase(cost_eur=400)], total_cost_eur=900)

    message = str(excinfo.value)
    assert "400" in message and "900" in message


def test_low_confidence_without_the_prefix_is_rejected():
    with pytest.raises(ValidationError, match="Out of scope"):
        result(summary="Not enough detail to size this at all.", confidence_pct=10)


def test_low_confidence_with_the_prefix_is_accepted():
    estimation = result(
        summary="Out of scope: the brief is too vague to size yet.", confidence_pct=10
    )

    assert estimation.confidence_pct == 10


def test_confidence_exactly_at_the_threshold_needs_no_prefix():
    """30 is the boundary: below it the prefix is required, at it it is not."""
    estimation = result(summary="Sizable with reasonable assumptions.", confidence_pct=30)

    assert estimation.confidence_pct == 30


def test_phase_count_is_bounded():
    with pytest.raises(ValidationError):
        result(phases=[], total_cost_eur=0)
