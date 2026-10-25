"""HTTP-level tests for ``POST /search`` (Session 8).

``get_semantic_retriever`` is overridden with a fake; no Postgres, no OpenAI.
The k bounds live in ``SearchRequest`` (422 before any I/O) and an empty corpus
is a 200 with an empty result list.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.dependencies import get_semantic_retriever
from app.embedding_pipeline.schemas import SearchHit, SearchResponse

URL = "/search"


def make_hits() -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=156,
            document_id=12,
            chunk_type="budget_component",
            content="Backend service implementation with JWT-based authentication...",
            distance=0.231,
            metadata={"budget_id": "BUD-2024-001", "component_id": "AUTH-001"},
        ),
        SearchHit(
            chunk_id=201,
            document_id=3,
            chunk_type="budget_component",
            content="Payment gateway integration with PSD2 compliance...",
            distance=0.305,
            metadata={"budget_id": "BUD-2024-003", "component_id": "PAY-002"},
        ),
    ]


class FakeRetriever:
    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits if hits is not None else make_hits()
        self.calls: list[dict] = []

    async def search(self, *, query: str, k: int) -> SearchResponse:
        self.calls.append({"query": query, "k": k})
        return SearchResponse(query=query, k=k, search_time_ms=87, results=self.hits[:k])


@pytest.fixture
def wired(client: TestClient):
    def _wire(retriever) -> TestClient:
        main.app.dependency_overrides[get_semantic_retriever] = lambda: retriever
        return client

    yield _wire
    main.app.dependency_overrides.clear()


def test_search_returns_ranked_hits_with_full_contract(wired) -> None:
    fake = FakeRetriever()
    client = wired(fake)

    response = client.post(URL, json={"query": "OAuth for fintech", "k": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "OAuth for fintech"
    assert body["k"] == 5
    assert body["search_time_ms"] == 87
    distances = [hit["distance"] for hit in body["results"]]
    assert distances == sorted(distances)
    first = body["results"][0]
    assert set(first) == {
        "chunk_id",
        "document_id",
        "chunk_type",
        "content",
        "distance",
        "metadata",
    }
    assert fake.calls == [{"query": "OAuth for fintech", "k": 5}]


def test_search_k_defaults_to_5(wired) -> None:
    fake = FakeRetriever()
    client = wired(fake)

    response = client.post(URL, json={"query": "anything"})

    assert response.status_code == 200
    assert fake.calls[0]["k"] == 5


def test_search_respects_k(wired) -> None:
    client = wired(FakeRetriever())

    response = client.post(URL, json={"query": "anything", "k": 1})

    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


@pytest.mark.parametrize("k", [0, -1, 51, 999])
def test_search_k_out_of_bounds_returns_422(wired, k: int) -> None:
    fake = FakeRetriever()
    client = wired(fake)

    response = client.post(URL, json={"query": "anything", "k": k})

    assert response.status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize("payload", [{}, {"query": ""}, {"query": "x", "extra": 1}])
def test_search_invalid_body_returns_422(wired, payload) -> None:
    fake = FakeRetriever()
    client = wired(fake)

    assert client.post(URL, json=payload).status_code == 422
    assert fake.calls == []


def test_search_empty_corpus_returns_200_with_no_results(wired) -> None:
    client = wired(FakeRetriever(hits=[]))

    response = client.post(URL, json={"query": "anything", "k": 5})

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_search_retriever_unavailable_returns_500(wired) -> None:
    client = wired(None)

    response = client.post(URL, json={"query": "anything", "k": 5})

    assert response.status_code == 500
    assert response.json() == {"detail": "Embedding service is not available."}


def test_search_embedding_failure_returns_500_without_leaking_detail(wired) -> None:
    class ExplodingRetriever(FakeRetriever):
        async def search(self, **kwargs):
            raise RuntimeError("sk-secret embeddings API down")

    client = wired(ExplodingRetriever())

    response = client.post(URL, json={"query": "anything", "k": 5})

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to run semantic search."}
    assert "sk-secret" not in response.text


def test_search_route_is_documented_in_openapi(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert URL in spec["paths"]
    assert "post" in spec["paths"][URL]
