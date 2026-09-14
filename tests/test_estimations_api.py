"""End-to-end tests through the FastAPI app: router -> service -> response model."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.llm_service import MAX_TOKENS
from tests.factories import ESTIMATION_TEXT

TRANSCRIPTION = (
    "Client meeting: the client needs a ticket management platform with SSO login, "
    "SLA timers, email notifications and a reporting dashboard, delivered in three months."
)
ENDPOINT = "/api/v1/estimate"


@pytest.fixture
def client():
    """TestClient with the app's lifespan (startup and shutdown) executed."""
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint_reports_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_openapi_schema_is_served_for_the_docs_page(client):
    """The /docs page is only useful if the schema it loads is available."""
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert ENDPOINT in response.json()["paths"]


def test_estimate_returns_a_complete_estimation_response(client, llm):
    response = client.post(ENDPOINT, json={"transcription": TRANSCRIPTION})

    assert response.status_code == 200
    body = response.json()
    assert body["estimation"] == ESTIMATION_TEXT
    assert body["model"] == "deepseek-flash"
    assert body["provider"] == "deepseek"
    assert set(body["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}


def test_estimate_rejects_a_transcription_that_is_too_short(client):
    response = client.post(ENDPOINT, json={"transcription": "too short"})

    assert response.status_code == 422


def test_estimate_rejects_a_missing_transcription(client):
    response = client.post(ENDPOINT, json={})

    assert response.status_code == 422


def test_estimate_reports_provider_failures_as_http_500(client, llm):
    llm.fail_with(RuntimeError("upstream exploded"))

    response = client.post(ENDPOINT, json={"transcription": TRANSCRIPTION})

    assert response.status_code == 500
    assert "upstream exploded" in response.json()["detail"]


def test_truncated_generation_is_never_returned_as_an_empty_estimation(client, llm):
    """Regression: this used to be a 200 with estimation='' or a bare traceback."""
    llm.reply("", "length", completion_tokens=MAX_TOKENS, reasoning_tokens=MAX_TOKENS)

    response = client.post(ENDPOINT, json={"transcription": TRANSCRIPTION})

    assert response.status_code == 500
    assert "output cap" in response.json()["detail"]
    assert "estimation" not in response.json()
