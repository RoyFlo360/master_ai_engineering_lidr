"""Integration tests for the conversational session endpoints.

The whole HTTP path is exercised through the real app: routing, multipart
parsing, attachment extraction, error mapping and the response model. Only the
two external seams are replaced — the session store (a fresh in-memory one per
test) and the estimation service (a fake, so no provider is ever called).
"""

from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app import main
from app.dependencies import get_estimation_service, get_session_store
from app.guardrails.input import InputGuardrailViolation
from app.routers import sessions as sessions_router
from app.schemas.new_estimation_schema import (
    EstimationResult,
    Phase,
    StructuredEstimationResponse,
)
from app.sessions.store import SessionStore
from tests.factories import build_settings

TRANSCRIPT = "A marketplace app for second-hand furniture with search and checkout."
ESTIMATE_URL = "/sessions/{session_id}/estimate"


def estimation() -> EstimationResult:
    return EstimationResult(
        summary="A short but sufficiently long overall summary.",
        confidence_pct=80,
        phases=[
            Phase(
                name="Discovery",
                duration_weeks=1,
                cost_eur=500,
                summary="Discovery workshops and scope definition.",
            )
        ],
        total_duration_weeks=1,
        total_cost_eur=500,
    )


def docx_bytes(*paragraphs: str) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class FakeEstimationService:
    """Records how the router called it; returns a valid structured response."""

    def __init__(self) -> None:
        self.seen: dict | None = None
        self.error: Exception | None = None
        self.result = estimation()

    def estimate_conversational(
        self,
        *,
        session,
        transcript,
        project_type,
        detail_level,
        output_format,
        tier=None,
        attachments_total_chars=0,
    ):
        self.seen = {
            "session": session,
            "transcript": transcript,
            "project_type": project_type,
            "detail_level": detail_level,
            "output_format": output_format,
            "tier": tier,
            "attachments_total_chars": attachments_total_chars,
        }
        if self.error is not None:
            raise self.error
        return StructuredEstimationResponse(
            result=self.result, prompt_version="v3", cache_hit=False
        )


@pytest.fixture(autouse=True)
def sessions_settings(monkeypatch):
    """Keep the router off the developer's real ``.env``."""
    monkeypatch.setattr(sessions_router, "get_settings", lambda: build_settings())


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def store():
    fresh = SessionStore(max_turns=6)
    main.app.dependency_overrides[get_session_store] = lambda: fresh
    yield fresh
    main.app.dependency_overrides.pop(get_session_store, None)


@pytest.fixture
def service():
    fake = FakeEstimationService()
    main.app.dependency_overrides[get_estimation_service] = lambda: fake
    yield fake
    main.app.dependency_overrides.pop(get_estimation_service, None)


def create_session(client) -> str:
    response = client.post("/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def estimate_form(**overrides) -> dict:
    return {
        "transcript": TRANSCRIPT,
        "project_type": "web_saas",
        "detail_level": "medium",
        "output_format": "phases_table",
        **overrides,
    }


# --- session lifecycle ------------------------------------------------------


def test_creating_a_session_returns_its_id(client, store):
    body = client.post("/sessions").json()

    assert body.keys() == {"session_id"}
    assert store.get(body["session_id"]) is not None


def test_getting_a_session_reports_its_metadata(client, store):
    session_id = create_session(client)

    body = client.get(f"/sessions/{session_id}").json()

    assert body["session_id"] == session_id
    assert body["message_count"] == 0
    assert body["max_turns"] == 6
    assert body["metadata"] == {
        "project_name": None,
        "assumed_team_size": None,
        "mentioned_technologies": [],
        "agreed_scope": None,
    }


def test_an_unknown_session_is_a_404(client, store):
    assert client.get("/sessions/does-not-exist").status_code == 404


# --- estimation -------------------------------------------------------------


def test_estimating_in_a_session_returns_the_structured_response(client, store, service):
    session_id = create_session(client)

    response = client.post(ESTIMATE_URL.format(session_id=session_id), data=estimate_form())

    assert response.status_code == 200
    body = response.json()
    assert body["prompt_version"] == "v3"
    assert body["cache_hit"] is False
    assert body["result"]["total_cost_eur"] == 500
    assert service.seen["transcript"] == TRANSCRIPT
    assert service.seen["project_type"].value == "web_saas"
    assert service.seen["detail_level"].value == "medium"
    assert service.seen["output_format"].value == "phases_table"


def test_estimating_in_an_unknown_session_is_a_404(client, store, service):
    response = client.post(
        ESTIMATE_URL.format(session_id="nope"), data=estimate_form()
    )

    assert response.status_code == 404
    assert service.seen is None


def test_a_missing_transcript_is_a_422(client, store, service):
    response = client.post(
        ESTIMATE_URL.format(session_id=create_session(client)),
        data=estimate_form(transcript="too short"),
    )

    assert response.status_code == 422


# --- attachments ------------------------------------------------------------


def test_an_unsupported_attachment_is_a_415(client, store, service):
    response = client.post(
        ESTIMATE_URL.format(session_id=create_session(client)),
        data=estimate_form(),
        files={"attachments": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 415
    assert response.json()["detail"]["reason"] == "unsupported_attachment"
    assert response.json()["detail"]["filename"] == "notes.txt"
    assert service.seen is None


def test_a_corrupt_pdf_is_a_422(client, store, service):
    response = client.post(
        ESTIMATE_URL.format(session_id=create_session(client)),
        data=estimate_form(),
        files={"attachments": ("broken.pdf", b"not a pdf", "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "attachment_extraction_failed"
    assert service.seen is None


def test_a_docx_attachment_is_extracted_into_the_transcript(client, store, service):
    response = client.post(
        ESTIMATE_URL.format(session_id=create_session(client)),
        data=estimate_form(),
        files={
            "attachments": (
                "brief.docx",
                docx_bytes("The client also wants offline mode."),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 200
    assert "--- attachment: brief.docx ---" in service.seen["transcript"]
    assert "The client also wants offline mode." in service.seen["transcript"]


# --- error mapping ----------------------------------------------------------


def test_a_guardrail_violation_is_a_400_with_its_reason(client, store, service):
    service.error = InputGuardrailViolation(
        "Suspicious instruction-like text detected.", reason="prompt_injection"
    )

    response = client.post(
        ESTIMATE_URL.format(session_id=create_session(client)), data=estimate_form()
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "reason": "prompt_injection",
        "message": "Suspicious instruction-like text detected.",
    }


def test_an_unexpected_failure_is_a_502(client, store, service):
    service.error = RuntimeError("provider exploded")

    response = client.post(
        ESTIMATE_URL.format(session_id=create_session(client)), data=estimate_form()
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream LLM call failed"


# --- the response model is the structured one, not the free-text one --------


def test_the_openapi_declares_the_structured_response_model(client):
    spec = client.get("/openapi.json").json()
    ref = spec["paths"]["/sessions/{session_id}/estimate"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"]

    assert ref.endswith("/StructuredEstimationResponse")
