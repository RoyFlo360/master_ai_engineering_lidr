"""End-to-end tests through the FastAPI app: router -> service -> response model."""

from json import loads
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_llm_wrapper
from app.main import app
from app.services.llm_service import MAX_TOKENS
from app.services.llm_wrapper import _usage_event
from tests.factories import ESTIMATION_TEXT

TRANSCRIPTION = (
    "Client meeting: the client needs a ticket management platform with SSO login, "
    "SLA timers, email notifications and a reporting dashboard, delivered in three months."
)
ENDPOINT = "/api/v1/estimate"
STREAM_ENDPOINT = "/api/v1/estimate/stream"


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
    assert set(body["usage"]) == {"input_tokens", "output_tokens", "total_tokens", "cost_usd"}


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


# --- streaming endpoint -----------------------------------------------------


class FakeStreamWrapper:
    """Stands in for LLMWrapper: streams one token, then reports usage."""

    max_tokens = 4321  # deliberately not 8000, so the fallback path is visible

    def complete_stream(
        self, *, system_prompt, user_message, max_tokens, model_override=None
    ):
        self.seen = {"max_tokens": max_tokens, "model_override": model_override}
        yield {"event": "token", "data": "## Inventory Platform"}
        yield _usage_event(
            "deepseek-flash",
            SimpleNamespace(prompt_tokens=2063, completion_tokens=812, total_tokens=2875),
        )


@pytest.fixture
def stream_wrapper():
    """Inject a fake wrapper so no test reaches a provider."""
    fake = FakeStreamWrapper()
    app.dependency_overrides[get_llm_wrapper] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_llm_wrapper, None)


def frame_data(sse_text: str, event: str) -> list[str]:
    """Return the ``data:`` payloads belonging to one SSE event name."""
    payloads: list[str] = []
    current = None
    for line in sse_text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current == event:
            payloads.append(line.split(":", 1)[1].strip())
    return payloads


def test_stream_accepts_a_transcription_without_any_usage_field(client, stream_wrapper):
    """Regression: requiring `usage` in the request rejected every call with a 422."""
    response = client.post(STREAM_ENDPOINT, json={"transcription": TRANSCRIPTION})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert frame_data(response.text, "token") == ["## Inventory Platform"]


def test_stream_reports_usage_and_cost_after_the_tokens(client, stream_wrapper):
    response = client.post(STREAM_ENDPOINT, json={"transcription": TRANSCRIPTION})

    usage = loads(frame_data(response.text, "usage")[0])
    assert set(usage) == {"input_tokens", "output_tokens", "total_tokens", "cost_usd"}
    assert usage["total_tokens"] == 2875
    # 2063 in at $0.15/M + 812 out at $0.60/M, from MODEL_COSTS.
    assert usage["cost_usd"] == 0.000797


def test_stream_uses_the_service_cap_when_the_request_omits_max_tokens(
    client, stream_wrapper
):
    """max_tokens has one default; omitting it must not shrink the cap."""
    client.post(STREAM_ENDPOINT, json={"transcription": TRANSCRIPTION})

    assert stream_wrapper.seen["max_tokens"] == stream_wrapper.max_tokens
