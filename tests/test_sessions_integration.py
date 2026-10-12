"""Async integration tests for the conversational session flow.

These drive the real app over ``httpx.AsyncClient`` + ``ASGITransport``: routing,
multipart parsing, attachment extraction, the sliding window, the heuristic
metadata refresh and error mapping all run for real. Only the two external
seams are replaced — the session store (a fresh in-memory one) and the LLM
wrapper (a recorder), both through ``app.dependency_overrides``.

The six scenarios the Session-5 brief asks for: a two-turn metadata update, a
PDF that changes the result, an eight-turn window check, empty values, weird
values, and a performance smoke.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.dependencies import get_estimation_service, get_session_store
from app.routers import sessions as sessions_router
from app.schemas.new_estimation_schema import EstimationResult, Phase
from app.services.estimation import EstimationService
from app.sessions.models import ConversationHistory, ProjectMetadata
from app.sessions.store import SessionStore
from tests.factories import build_settings

TRANSCRIPT = "A marketplace app for second-hand furniture with search and checkout."
MAX_TURNS = 6


# --- fixtures and fakes -----------------------------------------------------


def estimation(
    *,
    phase_name: str = "Implementation",
    summary: str = "A marketplace app for second-hand furniture.",
    confidence_pct: int = 80,
) -> EstimationResult:
    return EstimationResult(
        summary=summary,
        confidence_pct=confidence_pct,
        phases=[
            Phase(
                name=phase_name,
                duration_weeks=1,
                cost_eur=500,
                summary="Implementation work happens in this phase.",
            )
        ],
        total_duration_weeks=1,
        total_cost_eur=500,
    )


def pdf_aware(prompt: str) -> EstimationResult:
    """A deterministic 'model' that reacts to the attachment text."""
    if "offline mode" in prompt.lower():
        return estimation(phase_name="Offline sync")
    return estimation(phase_name="Core app")


def minimal_pdf(text: str) -> bytes:
    """A valid one-page PDF with a single text object (ASCII only)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def meta() -> dict:
    return {"model": "deepseek-flash", "provider": "deepseek", "latency_ms": 1}


class RecordingWrapper:
    """Fake ``LLMWrapper`` that records every messages array it is given."""

    def __init__(self, result_factory=None) -> None:
        self.primary_model = "deepseek/deepseek-flash"
        self.calls: list[dict] = []
        self.metadata_response = ProjectMetadata()
        self._result_factory = result_factory or (lambda _prompt: estimation())

    def complete_structured_chat(self, *, messages, response_model, **kwargs):
        self.calls.append({"messages": messages, "response_model": response_model})
        if response_model is EstimationResult:
            return self._result_factory(messages[-1]["content"]), meta()
        if response_model is ProjectMetadata:
            return self.metadata_response, meta()
        # The compression summarizer's private ``_SummaryEnvelope``: answer
        # with a stand-in that exposes ``.summary``.
        from types import SimpleNamespace

        return SimpleNamespace(summary="Earlier turns summarised."), meta()

    def complete_structured(self, *, system_prompt, user_message, response_model, **kwargs):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        self.calls.append({"messages": messages, "response_model": response_model})
        return self._result_factory(user_message), meta()


class NullCache:
    def get(self, key):  # pragma: no cover - the conversational path never caches
        return None

    def set(self, key, value):  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def router_settings(monkeypatch):
    """Keep the router off the developer's real ``.env``."""
    monkeypatch.setattr(sessions_router, "get_settings", lambda: build_settings())


@pytest.fixture
def wrapper() -> RecordingWrapper:
    return RecordingWrapper()


@pytest.fixture
def store() -> SessionStore:
    return SessionStore(max_turns=MAX_TURNS)


@pytest.fixture(autouse=True)
def overrides(store, wrapper):
    service = EstimationService(
        llm_wrapper=wrapper,
        exact_cache=NullCache(),
        semantic_cache=None,
        openai_client=None,
        metadata_extractor_model="gpt-4o-mini",
        enable_llm_metadata_fallback=True,
    )
    main.app.dependency_overrides[get_session_store] = lambda: store
    main.app.dependency_overrides[get_estimation_service] = lambda: service
    yield
    main.app.dependency_overrides.pop(get_session_store, None)
    main.app.dependency_overrides.pop(get_estimation_service, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# --- helpers ----------------------------------------------------------------


async def create_session(client: AsyncClient) -> str:
    response = await client.post("/sessions")
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


async def estimate(client: AsyncClient, session_id: str, transcript: str, files=None, **overrides) -> dict:
    response = await client.post(
        f"/sessions/{session_id}/estimate",
        data=estimate_form(transcript=transcript, **overrides),
        files=files,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- 1. two linked turns update project_metadata ----------------------------


@pytest.mark.anyio
async def test_two_turns_accumulate_project_metadata(client, store):
    session_id = await create_session(client)

    await estimate(client, session_id, "A marketplace app called Furnish, built with FastAPI.")
    first = (await client.get(f"/sessions/{session_id}")).json()
    assert first["metadata"]["project_name"] == "Furnish"
    assert "FastAPI" in first["metadata"]["mentioned_technologies"]

    await estimate(client, session_id, "Please add Stripe checkout to Furnish.")
    second = (await client.get(f"/sessions/{session_id}")).json()

    assert second["metadata"]["project_name"] == "Furnish"
    assert {"FastAPI", "Stripe"} <= set(second["metadata"]["mentioned_technologies"])
    assert second["metadata"]["agreed_scope"]  # the latest estimation summary
    assert second["message_count"] == 4  # two turns -> four messages


# --- 2. an attachment changes the estimate ----------------------------------


@pytest.mark.anyio
async def test_a_pdf_attachment_changes_the_result(client, store, wrapper):
    wrapper._result_factory = pdf_aware

    plain_session = await create_session(client)
    plain = await estimate(client, plain_session, TRANSCRIPT)

    pdf_session = await create_session(client)
    with_pdf = await estimate(
        client,
        pdf_session,
        TRANSCRIPT,
        files={
            "attachments": (
                "requirements.pdf",
                minimal_pdf("The client also wants offline mode."),
                "application/pdf",
            )
        },
    )

    assert plain["result"]["phases"][0]["name"] == "Core app"
    assert with_pdf["result"]["phases"][0]["name"] == "Offline sync"
    # The causal link: the PDF text reached the prompt.
    last_prompt = wrapper.calls[-1]["messages"][-1]["content"]
    assert "offline mode" in last_prompt.lower()
    assert "--- attachment: requirements.pdf ---" in last_prompt


# --- 3. the window never exceeds MAX_TURNS ----------------------------------


@pytest.mark.anyio
async def test_eight_turns_never_send_more_than_max_turns(client, store, wrapper):
    session_id = await create_session(client)
    for turn in range(8):
        await estimate(client, session_id, f"Turn {turn}: a marketplace for furniture item {turn}.")

    estimation_calls = [c for c in wrapper.calls if c["response_model"] is EstimationResult]
    assert len(estimation_calls) == 8
    for call in estimation_calls:
        assert call["messages"][0]["role"] == "system"  # system prompt always first
        history_and_current = [m for m in call["messages"] if m["role"] != "system"]
        # The composed context is bounded by: summary envelope (0 or 1) +
        # sliding window (<= 2 * MAX_TURNS) + the current user turn. Anchors
        # are unbounded by design and empty in this scenario.
        assert len(history_and_current) <= 2 * MAX_TURNS + 2

    session = (await client.get(f"/sessions/{session_id}")).json()
    assert session["message_count"] == 2 * MAX_TURNS  # oldest pairs dropped
    assert session["max_turns"] == MAX_TURNS

    last_prompt = " ".join(m["content"] for m in wrapper.calls[-1]["messages"])
    assert "item 0" not in last_prompt  # the first turn fell out of the window
    assert "item 7" in last_prompt


# --- 4. empty values --------------------------------------------------------


@pytest.mark.anyio
async def test_empty_values_are_rejected_or_ignored(client, store):
    session_id = await create_session(client)
    url = f"/sessions/{session_id}/estimate"

    missing = await client.post(url, data={k: v for k, v in estimate_form().items() if k != "transcript"})
    assert missing.status_code == 422

    for empty in ("", "   "):
        response = await client.post(url, data=estimate_form(transcript=empty))
        assert response.status_code == 422

    unknown_enum = await client.post(url, data=estimate_form(project_type="not_a_type"))
    assert unknown_enum.status_code == 422

    # An empty attachments list is simply no attachments.
    no_files = await client.post(url, data=estimate_form())
    assert no_files.status_code == 200

    # An empty file is rejected by the extractor, not silently accepted.
    empty_pdf = await client.post(
        url,
        data=estimate_form(),
        files={"attachments": ("empty.pdf", b"", "application/pdf")},
    )
    assert empty_pdf.status_code == 422

    unknown_session = await client.post("/sessions/nope/estimate", data=estimate_form())
    assert unknown_session.status_code == 404


# --- 5. weird values --------------------------------------------------------


@pytest.mark.anyio
async def test_weird_values_survive_the_round_trip(client, store, wrapper):
    session_id = await create_session(client)
    weird = "Build «Ω» プロジェクト 🚀 — naïve café, symbols: <>&%$#@!*()"

    body = await estimate(client, session_id, weird)

    assert body["result"]["total_cost_eur"] == 500
    prompt = wrapper.calls[-1]["messages"][-1]["content"]
    assert "🚀" in prompt and "プロジェクト" in prompt and "naïve café" in prompt

    # A weird filename is echoed back on failure, not a crash.
    response = await client.post(
        f"/sessions/{session_id}/estimate",
        data=estimate_form(),
        files={"attachments": ("資料📄.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["filename"] == "資料📄.pdf"


# --- 6. performance smoke ---------------------------------------------------


@pytest.mark.anyio
async def test_performance_smoke(client, store, wrapper):
    session_id = await create_session(client)

    started = time.perf_counter()
    for turn in range(8):
        await estimate(client, session_id, f"Turn {turn}: a marketplace for furniture.")
    elapsed = time.perf_counter() - started

    # Loose on purpose: this runs in-process against a fake LLM, so a slow run
    # means something pathological (a real regression is caught by the bound).
    assert elapsed < 5.0

    # Deterministic companion assertion: the window is bounded regardless of how
    # long the conversation gets, so trim cost stays constant.
    history = ConversationHistory(max_turns=MAX_TURNS)
    for index in range(5_000):
        history.append(user=f"u{index}", assistant=f"a{index}")
    # Bounding is the compression policy's job; the plain window helper keeps
    # the same invariant when called (see tests/test_compression_policy.py).
    history._trim()
    assert len(history.messages) == 2 * MAX_TURNS
