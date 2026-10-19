"""HTTP tests for ``POST /embeddings/ingest``.

The router, the real chunker and the real schemas are exercised; only the
embedder is replaced through ``app.dependency_overrides``. No provider is ever
called.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.dependencies import get_chunker, get_embedder
from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.schemas import Chunk, EmbeddedChunk

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"
URL = "/embeddings/ingest"


def sample_payload() -> dict:
    return {"budgets": json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))}


def minimal_budget(budget_id: str = "BUD-1", component_id: str = "C-1") -> dict:
    return {
        "budget_id": budget_id,
        "client_metadata": {"name": "Acme", "sector": "finance", "country": "ES"},
        "project_summary": "A small API.",
        "main_technology": "python_fastapi",
        "year": 2024,
        "total_estimated_hours": 5,
        "components": [
            {
                "component_id": component_id,
                "name": "Auth",
                "description": "Token-based authentication.",
                "tech_stack": ["python_fastapi"],
                "estimated_hours": 5,
                "complexity": "low",
                "dependencies": [],
            }
        ],
    }


class FakeEmbedder:
    """Returns a deterministic vector per chunk; records what it was given."""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.seen: list[list[Chunk]] = []

    def embed_many(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        self.seen.append(list(chunks))
        return [
            EmbeddedChunk(**chunk.model_dump(), embedding=[0.5] * self.dim)
            for chunk in chunks
        ]


class RaisingEmbedder:
    def __init__(self, message: str = "provider exploded") -> None:
        self.message = message

    def embed_many(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        raise RuntimeError(self.message)


@pytest.fixture
def wired(client: TestClient):
    """Wire the router to a fake embedder and the real chunker."""

    def _wire(embedder) -> TestClient:
        main.app.dependency_overrides[get_chunker] = lambda: JSONStructuralChunker()
        main.app.dependency_overrides[get_embedder] = lambda: embedder
        return client

    yield _wire
    main.app.dependency_overrides.clear()


def test_sample_returns_one_embedded_chunk_per_component(wired) -> None:
    embedder = FakeEmbedder()
    client = wired(embedder)

    response = client.post(URL, json=sample_payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["chunks"]) == 52
    assert body["stats"]["total_budgets"] == 15
    assert body["stats"]["total_chunks"] == 52
    assert body["stats"]["total_tokens"] > 0
    assert body["stats"]["estimated_cost_usd"] >= 0.0


def test_stats_are_consistent_with_the_chunks(wired) -> None:
    client = wired(FakeEmbedder())
    body = client.post(URL, json=sample_payload()).json()

    assert body["stats"]["total_chunks"] == len(body["chunks"])
    assert body["stats"]["total_tokens"] == sum(
        chunk["token_count"] for chunk in body["chunks"]
    )
    expected_cost = body["stats"]["total_tokens"] / 1_000_000 * 0.02
    assert body["stats"]["estimated_cost_usd"] == pytest.approx(expected_cost)


def test_chunk_order_matches_the_request(wired) -> None:
    client = wired(FakeEmbedder())
    body = client.post(URL, json=sample_payload()).json()
    chunk_ids = [chunk["chunk_id"] for chunk in body["chunks"]]
    assert chunk_ids[0].startswith("BUD-2024-001::")
    assert len(set(chunk_ids)) == len(chunk_ids)


def test_every_chunk_carries_a_full_vector(wired) -> None:
    client = wired(FakeEmbedder(dim=8))
    body = client.post(URL, json=sample_payload()).json()
    assert all(len(chunk["embedding"]) == 8 for chunk in body["chunks"])
    assert all(chunk["metadata"]["budget_id"] for chunk in body["chunks"])


def test_single_budget_round_trip(wired) -> None:
    client = wired(FakeEmbedder())
    response = client.post(URL, json={"budgets": [minimal_budget()]})
    assert response.status_code == 200
    body = response.json()
    assert body["chunks"][0]["chunk_id"] == "BUD-1::C-1"
    assert body["stats"]["total_chunks"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"budgets": []},
        {},
        {"budgets": [minimal_budget()], "unknown": 1},
        {"budgets": [minimal_budget(), minimal_budget()]},
    ],
)
def test_invalid_payloads_return_422(wired, payload) -> None:
    client = wired(FakeEmbedder())
    assert client.post(URL, json=payload).status_code == 422


def test_unknown_sector_returns_422(wired) -> None:
    client = wired(FakeEmbedder())
    payload = {"budgets": [minimal_budget()]}
    payload["budgets"][0]["client_metadata"]["sector"] = "fintech"
    assert client.post(URL, json=payload).status_code == 422


def test_missing_embedder_returns_generic_500(wired) -> None:
    client = wired(None)
    response = client.post(URL, json=sample_payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "Embedding service is not available."}


def test_provider_failure_returns_generic_500_without_leaking_detail(wired) -> None:
    client = wired(RaisingEmbedder(message="sk-secret-key exploded in provider"))
    response = client.post(URL, json=sample_payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to generate embeddings."}
    assert "sk-secret" not in response.text
    assert "exploded" not in response.text


def test_route_is_documented_in_openapi(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert URL in spec["paths"]
    assert "post" in spec["paths"][URL]


def test_chunker_is_given_the_validated_budgets(wired) -> None:
    embedder = FakeEmbedder()
    client = wired(embedder)
    client.post(URL, json=sample_payload())
    # One call, 52 chunks, and no chunk duplicated by the router.
    assert len(embedder.seen) == 1
    assert len(embedder.seen[0]) == 52
