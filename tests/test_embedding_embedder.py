"""Embedder tests.

Hermetic: a fake embeddings client, a patched ``RateLimitError`` and a recorded
``time.sleep``. No provider call, no real waiting.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

import app.embedding_pipeline.embedder as embedder_module
from app.embedding_pipeline.embedder import (
    BATCH_SIZE,
    PRICE_PER_MILLION_TOKENS_USD,
    EmbeddingCountMismatchError,
    OpenAIEmbedder,
    estimated_cost_usd,
)
from app.embedding_pipeline.schemas import Chunk


class FakeRateLimitError(Exception):
    """Stand-in for ``openai.RateLimitError``; the module looks it up by name."""


class FakeEmbeddingsAPI:
    def __init__(
        self,
        *,
        dim: int = 4,
        fail_first: int = 0,
        error: Exception | None = None,
        drop_vectors: int = 0,
    ) -> None:
        self.dim = dim
        self.fail_first = fail_first
        self.error = error
        self.drop_vectors = drop_vectors
        self.calls: list[dict] = []

    def create(self, *, model: str, input: list[str]):  # noqa: A002 - API name
        self.calls.append({"model": model, "input": list(input)})
        if self.fail_first > 0:
            self.fail_first -= 1
            raise FakeRateLimitError("slow down")
        if self.error is not None:
            raise self.error
        count = len(input) - self.drop_vectors
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[float(i)] * self.dim) for i in range(count)
            ]
        )


class FakeClient:
    def __init__(self, api: FakeEmbeddingsAPI) -> None:
        self.embeddings = api


@pytest.fixture(autouse=True)
def patch_rate_limit_error(monkeypatch):
    monkeypatch.setattr(embedder_module, "RateLimitError", FakeRateLimitError)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(embedder_module.time, "sleep", recorded.append)
    return recorded


def make_chunks(count: int) -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"BUD::{n}",
            text=f"text {n}",
            metadata={"n": n},
            token_count=n + 1,
        )
        for n in range(count)
    ]


def test_embed_one_makes_a_single_call() -> None:
    api = FakeEmbeddingsAPI(dim=3)
    vector = OpenAIEmbedder(FakeClient(api)).embed_one("hello")
    assert len(api.calls) == 1
    assert api.calls[0]["input"] == ["hello"]
    assert vector == [0.0, 0.0, 0.0]


@pytest.mark.parametrize(
    ("chunk_count", "expected_calls"),
    [(0, 0), (1, 1), (99, 1), (100, 1), (101, 2), (250, 3)],
)
def test_batching_matches_the_batch_size(
    chunk_count: int, expected_calls: int
) -> None:
    api = FakeEmbeddingsAPI()
    OpenAIEmbedder(FakeClient(api)).embed_many(make_chunks(chunk_count))
    assert len(api.calls) == expected_calls
    assert BATCH_SIZE == 100


def test_batches_never_exceed_the_batch_size() -> None:
    api = FakeEmbeddingsAPI()
    OpenAIEmbedder(FakeClient(api)).embed_many(make_chunks(250))
    assert [len(call["input"]) for call in api.calls] == [100, 100, 50]


def test_order_is_preserved() -> None:
    api = FakeEmbeddingsAPI(dim=1)
    chunks = make_chunks(150)
    embedded = OpenAIEmbedder(FakeClient(api)).embed_many(chunks)
    assert [chunk.chunk_id for chunk in embedded] == [c.chunk_id for c in chunks]
    assert embedded[0].metadata == {"n": 0}
    assert embedded[-1].metadata == {"n": 149}


def test_metadata_and_token_count_survive_embedding() -> None:
    chunk = make_chunks(1)[0]
    embedded = OpenAIEmbedder(FakeClient(FakeEmbeddingsAPI())).embed_many([chunk])[0]
    assert embedded.metadata == chunk.metadata
    assert embedded.token_count == chunk.token_count
    assert len(embedded.embedding) == 4


def test_rate_limit_is_retried_with_the_documented_schedule(sleeps) -> None:
    api = FakeEmbeddingsAPI(fail_first=2)
    embedded = OpenAIEmbedder(FakeClient(api)).embed_many(make_chunks(1))
    assert len(embedded) == 1
    assert sleeps == [1, 2]
    assert len(api.calls) == 3


def test_rate_limit_exhaustion_raises_after_three_retries(sleeps) -> None:
    api = FakeEmbeddingsAPI(fail_first=4)
    with pytest.raises(FakeRateLimitError):
        OpenAIEmbedder(FakeClient(api)).embed_many(make_chunks(1))
    assert sleeps == [1, 2, 4]
    assert len(api.calls) == 4  # one initial attempt + three retries


def test_non_rate_limit_errors_propagate_immediately(sleeps) -> None:
    api = FakeEmbeddingsAPI(error=ValueError("bad request"))
    with pytest.raises(ValueError, match="bad request"):
        OpenAIEmbedder(FakeClient(api)).embed_many(make_chunks(1))
    assert sleeps == []
    assert len(api.calls) == 1


def test_vector_count_mismatch_raises_instead_of_dropping_chunks() -> None:
    api = FakeEmbeddingsAPI(drop_vectors=2)
    with pytest.raises(EmbeddingCountMismatchError):
        OpenAIEmbedder(FakeClient(api)).embed_many(make_chunks(5))


def test_cost_math() -> None:
    assert PRICE_PER_MILLION_TOKENS_USD == 0.02
    assert estimated_cost_usd(0) == 0.0
    assert math.isclose(estimated_cost_usd(1_000_000), 0.02)
    assert math.isclose(estimated_cost_usd(4_100), 0.000082)


def test_empty_input_makes_no_call(sleeps) -> None:
    api = FakeEmbeddingsAPI()
    assert OpenAIEmbedder(FakeClient(api)).embed_many([]) == []
    assert api.calls == []
    assert sleeps == []


def test_batch_logging_reports_counts_tokens_and_latency(monkeypatch) -> None:
    events: list[tuple[str, dict]] = []

    class Recorder:
        def info(self, event, **kwargs):
            events.append((event, kwargs))

        def warning(self, event, **kwargs):
            events.append((event, kwargs))

    monkeypatch.setattr(embedder_module, "log", Recorder())
    OpenAIEmbedder(FakeClient(FakeEmbeddingsAPI())).embed_many(make_chunks(3))
    assert [event for event, _ in events] == ["embedding_batch_done"]
    _, kwargs = events[0]
    assert kwargs["chunks"] == 3
    assert kwargs["tokens"] == 1 + 2 + 3
    assert "latency_ms" in kwargs
    assert kwargs["model"] == "text-embedding-3-small"
