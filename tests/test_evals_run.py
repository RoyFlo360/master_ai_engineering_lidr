"""Smoke test: the ``evals`` runner end-to-end with a scripted wrapper.

We mount the conversational dependencies onto the app via dependency_overrides
(same pattern as the integration tests), drive the in-process transport, and
assert metrics flow through. A real LLM is never called.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.dependencies import get_estimation_service, get_session_store
from app.main import app
from app.schemas.new_estimation_schema import EstimationResult, Phase
from app.schemas.critic import CriticFeedback
from app.services.estimation import EstimationService
from app.sessions.models import ProjectMetadata
from app.sessions.store import SessionStore
from evals.dataset import load_dataset
from evals.metrics import run_all_metrics
from evals.run import _endpoint_for, _evaluate


def _meta() -> dict:
    return {"model": "deepseek-flash", "provider": "deepseek", "latency_ms": 1}


class ScriptedWrapper:
    """Answers each structured call by ``response_model``."""

    def __init__(self, result: EstimationResult) -> None:
        self.primary_model = "deepseek/deepseek-flash"
        self._result = result

    def complete_structured_chat(self, *, messages, response_model, **kwargs):
        if response_model is EstimationResult:
            return self._result, _meta()
        if response_model is ProjectMetadata:
            return ProjectMetadata(), _meta()
        if response_model is CriticFeedback:
            return CriticFeedback(verdict="accept", issues=[], confidence_in_review=90), _meta()
        # Compression summarizer's private ``_SummaryEnvelope``.
        return SimpleNamespace(summary="Earlier turns summarised."), _meta()


def _canned(total: int = 30_000, weeks: int = 8) -> EstimationResult:
    return EstimationResult(
        summary="A web SaaS marketplace for gardeners, built with React and Stripe.",
        confidence_pct=75,
        phases=[
            Phase(name="Discovery", duration_weeks=1, cost_eur=5_000, summary="Scope the work fully."),
            Phase(name="Build", duration_weeks=weeks - 1, cost_eur=total - 5_000, summary="Build the app."),
        ],
        total_duration_weeks=weeks,
        total_cost_eur=total,
    )


def test_dataset_loads_with_at_least_15_cases() -> None:
    cases = load_dataset()
    assert len(cases) >= 15
    assert any(c.expected_out_of_scope for c in cases)


def test_runner_collects_metrics_per_case_for_both_modes() -> None:
    wrapper = ScriptedWrapper(_canned())
    store = SessionStore(max_turns=6)
    service = EstimationService(
        llm_wrapper=wrapper,
        exact_cache=None,
        semantic_cache=None,
        openai_client=None,
        metadata_extractor_model="gpt-4o-mini",
    )
    app.dependency_overrides[get_estimation_service] = lambda: service
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        cases = load_dataset()[:2]
        with TestClient(app) as client:
            for mode in ("actor", "acb"):
                for case in cases:
                    sid = client.post("/sessions").json()["session_id"]
                    form = {
                        "transcript": case.transcript,
                        "project_type": case.project_type,
                        "detail_level": case.detail_level,
                        "output_format": case.output_format,
                    }
                    response = client.post(_endpoint_for(sid, mode), data=form)
                    assert response.status_code == 200, response.text
                    results = _evaluate(case, response.json())
                    assert [r.name for r in results] == [
                        "schema_adherence",
                        "cost_bounds",
                        "content_recall",
                    ]
    finally:
        app.dependency_overrides.clear()


def test_runner_metrics_run_all_metrics_shape() -> None:
    case = load_dataset()[0]
    results = run_all_metrics(case, _canned())
    assert len(results) == 3
