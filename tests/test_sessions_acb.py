"""Integration test for POST /sessions/{id}/estimate-acb.

Drives the real ``EstimationService.estimate_with_acb`` through the real app
over ``TestClient``. Only the two external seams are replaced — an in-memory
session store and a scripted LLM wrapper that answers by ``response_model``.
No provider is ever reached.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_estimation_service, get_session_store
from app.main import app
from app.schemas.critic import CriticFeedback, CriticIssue
from app.schemas.new_estimation_schema import EstimationResult, Phase
from app.services.estimation import EstimationService
from app.sessions.models import ProjectMetadata
from app.sessions.store import SessionStore


VALID_FORM = {
    "transcript": "We need to estimate Nimbus, a CRM with React and Postgres for the sales team.",
    "project_type": "web_saas",
    "detail_level": "medium",
    "output_format": "phases_table",
}


def _meta() -> dict:
    return {"model": "deepseek-flash", "provider": "deepseek", "latency_ms": 1}


def _estimation() -> EstimationResult:
    return EstimationResult(
        summary="Nimbus CRM for the sales team.",
        confidence_pct=80,
        phases=[
            Phase(name="Discovery", duration_weeks=1, cost_eur=500, summary="Scope the CRM work."),
        ],
        total_duration_weeks=1,
        total_cost_eur=500,
    )


class ScriptedWrapper:
    """Answers each structured call by ``response_model`` name.

    ``critic_factory`` lets a test drive the Boss loop; it defaults to accept.
    """

    def __init__(self) -> None:
        self.primary_model = "deepseek/deepseek-flash"
        self.calls: list[dict] = []
        self.critic_factory = lambda: CriticFeedback(
            verdict="accept", issues=[], confidence_in_review=90
        )

    def complete_structured_chat(self, *, messages, response_model, **kwargs):
        self.calls.append({"messages": messages, "response_model": response_model})
        if response_model is EstimationResult:
            return _estimation(), _meta()
        if response_model is ProjectMetadata:
            return ProjectMetadata(), _meta()
        if response_model is CriticFeedback:
            return self.critic_factory(), _meta()
        # The compression summarizer's private ``_SummaryEnvelope``.
        return SimpleNamespace(summary="Earlier turns summarised."), _meta()


@pytest.fixture
def acb_client():
    wrapper = ScriptedWrapper()
    store = SessionStore(max_turns=6)
    service = EstimationService(
        llm_wrapper=wrapper,
        exact_cache=None,
        semantic_cache=None,
        openai_client=None,
        metadata_extractor_model="gpt-4o-mini",
        boss_max_iterations=2,
    )
    app.dependency_overrides[get_estimation_service] = lambda: service
    app.dependency_overrides[get_session_store] = lambda: store
    with TestClient(app) as client:
        yield client, store, wrapper
    app.dependency_overrides.clear()


def test_acb_endpoint_accepts_first_pass(acb_client) -> None:
    client, _store, _wrapper = acb_client
    session_id = client.post("/sessions").json()["session_id"]

    response = client.post(f"/sessions/{session_id}/estimate-acb", data=VALID_FORM)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["prompt_version"] == "v3"
    assert body["cache_hit"] is False
    assert "acb" in body
    assert body["acb"]["final_decision"] == "accept"
    assert body["acb"]["iterations_run"] == 1
    assert body["acb"]["iterations"][0]["critic_verdict"] == "accept"


def test_acb_endpoint_synthesizes_after_persistent_issues(acb_client) -> None:
    client, _store, wrapper = acb_client
    wrapper.critic_factory = lambda: CriticFeedback(
        verdict="needs_iteration",
        issues=[
            CriticIssue(
                category="math_error",
                severity="critical",
                field_path="total_cost_eur",
                description="sum mismatch in the totals",
                suggested_fix="recompute the sum",
            )
        ],
        confidence_in_review=80,
    )
    session_id = client.post("/sessions").json()["session_id"]

    response = client.post(f"/sessions/{session_id}/estimate-acb", data=VALID_FORM)
    assert response.status_code == 200, response.text
    body = response.json()

    # The Critic never accepts → Boss exhausts the budget and synthesizes.
    # The synthesize policy preserves the actor's last draft with caveats.
    assert body["acb"]["final_decision"] == "synthesize"
    assert body["acb"]["iterations_run"] == 2
    assert body["result"]["summary"].startswith("⚠ Open caveats")
    assert "math_error" in body["result"]["summary"]
    assert body["result"]["total_cost_eur"] > 0
    assert body["result"]["confidence_pct"] >= 30


def test_acb_endpoint_persists_only_final_result(acb_client) -> None:
    client, store, _wrapper = acb_client
    session_id = client.post("/sessions").json()["session_id"]
    client.post(f"/sessions/{session_id}/estimate-acb", data=VALID_FORM)

    session = store.get_or_404(session_id)
    # Exactly one turn lands in the history regardless of internal drafts.
    assert len(session.history.messages) == 2
    assert session.history.messages[0].role == "user"
    assert session.history.messages[1].role == "assistant"


def test_acb_endpoint_404_for_unknown_session(acb_client) -> None:
    client, _store, _wrapper = acb_client
    response = client.post("/sessions/does-not-exist/estimate-acb", data=VALID_FORM)
    assert response.status_code == 404
    assert response.json()["detail"] == "session_not_found"
