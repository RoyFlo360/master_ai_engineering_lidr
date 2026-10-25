"""Security tests for the embedding pipeline.

The endpoint is unauthenticated and every chunk costs money to embed, so the
tests pin: input caps, unknown-field rejection, identifier integrity, and the
rule that neither the response body nor the logs carry secrets or payload text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.embedding_pipeline.embedder as embedder_module
from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.embedder import OpenAIEmbedder
from app.embedding_pipeline.schemas import (
    MAX_DESCRIPTION_CHARS,
    MAX_SOURCE_PATH_CHARS,
    Budget,
    Chunk,
    IngestRequest,
)


def component(component_id: str, **overrides) -> dict:
    payload = {
        "component_id": component_id,
        "name": "Auth",
        "description": "Token-based authentication.",
        "tech_stack": [],
        "estimated_hours": 1,
        "complexity": "low",
        "dependencies": [],
    }
    payload.update(overrides)
    return payload


def budget(budget_id: str, components: list[dict]) -> dict:
    return {
        "budget_id": budget_id,
        "client_metadata": {"name": "Acme", "sector": "finance", "country": "ES"},
        "project_summary": "A small API.",
        "main_technology": "python_fastapi",
        "year": 2024,
        "total_estimated_hours": 1,
        "components": components,
    }


def test_source_path_cap_is_enforced_at_the_request_boundary() -> None:
    """`source_path` is stored and used as the uniqueness key, so it is bounded."""
    with pytest.raises(ValidationError):
        IngestRequest.model_validate(
            {
                "source_path": "x" * (MAX_SOURCE_PATH_CHARS + 1),
                "document_type": "historical_budget",
                "content": budget("BUD-1", [component("C-1")]),
            }
        )


def test_source_path_rejects_control_characters() -> None:
    for bad in ("a\tb", "a\nb", "a\x00b", " pad "):
        with pytest.raises(ValidationError):
            IngestRequest.model_validate(
                {
                    "source_path": bad,
                    "document_type": "historical_budget",
                    "content": budget("BUD-1", [component("C-1")]),
                }
            )


def test_component_count_per_budget_is_capped() -> None:
    too_many = [component(f"C-{i}") for i in range(101)]
    with pytest.raises(ValidationError):
        Budget.model_validate(budget("BUD-1", too_many))


def test_description_cap_is_enforced() -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate(
            budget(
                "BUD-1",
                [component("C-1", description="x" * (MAX_DESCRIPTION_CHARS + 1))],
            )
        )


def test_unknown_fields_are_rejected_at_every_level() -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate({**budget("BUD-1", [component("C-1")]), "extra": 1})
    with pytest.raises(ValidationError):
        Budget.model_validate(
            budget(
                "BUD-1",
                [{**component("C-1"), "extra": 1}],
            )
        )


def test_duplicate_component_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="component_id"):
        Budget.model_validate(
            budget("BUD-1", [component("C-1"), component("C-1")])
        )


def test_negative_and_absurd_hours_are_rejected() -> None:
    for hours in (-1, 100_001):
        with pytest.raises(ValidationError):
            Budget.model_validate(
                budget("BUD-1", [component("C-1", estimated_hours=hours)])
            )


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))

    def warning(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))


class FakeAPI:
    def create(self, *, model: str, input: list[str]):  # noqa: A002 - API name
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.0]) for _ in input]
        )


class FakeClient:
    def __init__(self) -> None:
        self.embeddings = FakeAPI()


def test_logs_carry_counts_but_never_chunk_text(monkeypatch) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(embedder_module, "log", recorder)

    secret = "launch-code-9F3A"
    chunks = [
        Chunk(
            chunk_id="BUD::C",
            text=f"Component: {secret}",
            metadata={},
            token_count=5,
        )
    ]
    OpenAIEmbedder(FakeClient()).embed_many(chunks)

    serialized = repr(recorder.events)
    assert secret not in serialized
    assert all("text" not in kwargs for _, kwargs in recorder.events)
    assert recorder.events[0][1]["chunks"] == 1


def test_no_api_key_reaches_the_logs(monkeypatch) -> None:
    recorder = RecordingLogger()
    monkeypatch.setattr(embedder_module, "log", recorder)
    OpenAIEmbedder(FakeClient(), model="text-embedding-3-small").embed_many(
        [Chunk(chunk_id="B::C", text="x", metadata={}, token_count=1)]
    )
    assert "sk-" not in repr(recorder.events)


def test_unicode_crlf_and_nul_bytes_do_not_break_chunking() -> None:
    payload = budget(
        "BUD-1",
        [component("C-1", description="café\r\nnul:\x00 end")],
    )
    chunk = JSONStructuralChunker().chunk([Budget.model_validate(payload)])[0]
    assert chunk.token_count >= 0
    assert "café" in chunk.text


def test_free_text_cannot_inject_metadata() -> None:
    payload = budget(
        "BUD-1",
        [component("C-1", description='{"budget_id": "INJECTED"}')],
    )
    chunk = JSONStructuralChunker().chunk([Budget.model_validate(payload)])[0]
    assert chunk.metadata["budget_id"] == "BUD-1"
    assert chunk.chunk_id == "BUD-1::C-1"


# Residual risk, deliberately not fixed here:
#   * the endpoint has no authentication and no rate limit, so it can spend
#     OpenAI credit;
#   * the caps bound one request, not a caller's total spend;
#   * embeddings are returned in full (~1.5 MB for the sample), so a permitted
#     request is still a large response.
# Adopting auth or rate limiting is out of scope for this exercise and is
# recorded in spec 010's follow-ups.
