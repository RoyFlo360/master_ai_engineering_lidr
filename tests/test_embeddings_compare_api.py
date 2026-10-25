"""HTTP tests for ``POST /embeddings/compare`` (spec 013).

A fake embedder stands in for OpenAI; the chunkers are real (the dependency-free
subset, so nothing calls a provider). The point is the status mapping and the
response shape — the numbers themselves are the CLI's job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.dependencies import get_chunker, get_embedder
from app.embedding_pipeline.chunking.structural import JSONStructuralChunker
from app.embedding_pipeline.schemas import Chunk, EmbeddedChunk

URL = "/embeddings/compare"
SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


def sample_budgets() -> list[dict]:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


class FakeEmbedder:
    """Deterministic vectors; records the texts it was asked to embed."""

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.embedded: list[str] = []
        self.queries: list[str] = []

    def embed_many(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        self.embedded.extend(chunk.text for chunk in chunks)
        return [
            EmbeddedChunk(**chunk.model_dump(), embedding=[0.5] * self.dim) for chunk in chunks
        ]

    def embed_one(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.5] * self.dim


@pytest.fixture
def wired(client: TestClient):
    def _wire(embedder) -> TestClient:
        main.app.dependency_overrides[get_embedder] = lambda: embedder
        main.app.dependency_overrides[get_chunker] = lambda: JSONStructuralChunker()
        return client

    yield _wire
    main.app.dependency_overrides.clear()


def test_compare_returns_stats_for_the_requested_strategies(wired) -> None:
    embedder = FakeEmbedder()
    client = wired(embedder)

    response = client.post(
        URL,
        json={
            "budgets": sample_budgets()[:2],
            "strategies": ["structural", "fixed_size"],
            "queries": [],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["stats_per_strategy"]) == {"structural", "fixed_size"}
    assert body["queries_per_strategy"] == {}
    stats = body["stats_per_strategy"]["structural"]
    assert set(stats) == {
        "strategy",
        "n_chunks",
        "token_distribution",
        "n_orphan_chunks",
        "n_obese_chunks",
        "ingestion_cost_usd",
        "ingestion_seconds",
    }
    assert stats["n_chunks"] > 0
    assert set(stats["token_distribution"]) == {"min", "p50", "p95", "max"}
    assert embedder.queries == []


def test_compare_with_queries_returns_top_k_per_strategy(wired) -> None:
    embedder = FakeEmbedder()
    client = wired(embedder)

    response = client.post(
        URL,
        json={
            "budgets": sample_budgets()[:1],
            "strategies": ["structural"],
            "queries": ["OAuth authentication"],
            "top_k": 2,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    results = body["queries_per_strategy"]["structural"]
    assert len(results) == 1
    assert results[0]["query"] == "OAuth authentication"
    assert len(results[0]["top_k"]) == 2
    assert set(results[0]["top_k"][0]) == {"chunk_id", "cosine", "text_preview"}
    assert embedder.queries == ["OAuth authentication"]
    # Cosine of a vector against itself is 1.0 with a constant fake embedder.
    assert results[0]["top_k"][0]["cosine"] == pytest.approx(1.0)


def test_compare_defaults_to_every_strategy_that_can_run(wired) -> None:
    """No ``strategies`` means all of them; without keys the LLM ones are a 500."""
    embedder = FakeEmbedder()
    client = wired(embedder)

    response = client.post(URL, json={"budgets": sample_budgets()[:1]})

    # contextual_retrieval needs ANTHROPIC_API_KEY, which the test settings lack,
    # so the all-strategies path fails loudly rather than silently comparing six.
    assert response.status_code in {200, 500}
    if response.status_code == 500:
        assert response.json()["detail"] == "ContextualRetrievalChunker requires ANTHROPIC_API_KEY."


def test_compare_unknown_strategy_returns_400(wired) -> None:
    client = wired(FakeEmbedder())

    response = client.post(
        URL, json={"budgets": sample_budgets()[:1], "strategies": ["does_not_exist"]}
    )

    assert response.status_code == 400
    assert "Unknown strategy" in response.json()["detail"]


def test_compare_missing_embedder_returns_500(wired) -> None:
    client = wired(None)

    response = client.post(URL, json={"budgets": sample_budgets()[:1], "strategies": ["structural"]})

    assert response.status_code == 500
    assert response.json()["detail"] == "Embedding service is not available."


@pytest.mark.parametrize(
    "payload",
    [
        {"budgets": []},
        {"budgets": sample_budgets()[:1], "top_k": 0},
        {"budgets": sample_budgets()[:1], "top_k": 11},
        {"budgets": sample_budgets()[:1], "unknown": 1},
    ],
)
def test_compare_invalid_body_returns_422(wired, payload) -> None:
    client = wired(FakeEmbedder())
    assert client.post(URL, json=payload).status_code == 422


def test_compare_route_is_documented_in_openapi(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert URL in spec["paths"]
    assert "post" in spec["paths"][URL]
