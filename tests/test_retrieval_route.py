"""HTTP-level tests for ``POST /v1/retrieval/search`` (Session 10).

Replaces the Session-8 ``tests/test_search_endpoint.py`` style with the same
seam: ``app.dependency_overrides`` for every collaborator the router declares —
settings, embedder, session factory, store and reranker. No Postgres, no OpenAI,
no torch; the store and the reranker are fakes, so every branch of
``pipeline.retrieve`` is exercised without a network with the *real* pipeline.

What this file pins:

* 200 for all four configurations (vector/hybrid x rerank on/off), with the body
  validated against ``RetrievalResult``;
* defaults resolved from ``Settings`` when ``search_mode``/``rerank``/``recall_k``
  are omitted, and an explicit request field winning over the setting;
* 422 for every documented body bound, before any I/O;
* **502 with the exact documented body** when the injected reranker raises, and
  **200 with the same broken reranker wired** when ``rerank=false`` — the
  failure must not leak into the un-reranked paths;
* 500 ``"Embedding service is not available."`` when the embedder resolves to
  ``None``, and the generic 500 for any other failure, with no exception text in
  the response.

``POST /search`` is deliberately not asserted here: it is retired in T6, and a
test that pins its presence today would have to be deleted tomorrow.
"""

from __future__ import annotations

import pytest

from app import main
from app.dependencies import get_chunk_store, get_embedder, get_reranker
from app.embedding_pipeline.schemas import MAX_SEARCH_K
from app.generation.rag.retrieval.router import (
    get_retrieval_session_factory,
    get_retrieval_settings,
)
from app.generation.rag.retrieval.schemas import RetrievalResult
from tests.factories import build_settings

URL = "/v1/retrieval/search"

RERANKER_ERROR = "Reranking service is unavailable. Please retry later."
EMBEDDER_ERROR = "Embedding service is not available."
GENERIC_ERROR = "Failed to run retrieval."

#: The contract's response fields. A new field is a client-visible change, so it
#: must be an explicit edit here, not a silent addition to the model.
RESULT_KEYS = {
    "chunks",
    "search_mode",
    "rerank",
    "candidates_evaluated",
    "search_time_ms",
    "low_confidence",
}
CHUNK_KEYS = {
    "chunk_id",
    "document_id",
    "budget_id",
    "component_id",
    "chunk_type",
    "content",
    "sector",
    "project_year",
    "distance",
    "rank",
    "source",
}

_UNSET = object()


# --- fakes -----------------------------------------------------------------


class FakeEmbedder:
    """Synchronous embedder, like ``OpenAIEmbedder.embed_one`` (run in a thread)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.01] * 8


class _SessionContext:
    """Async context manager standing in for ``AsyncSession``'s lifecycle."""

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


def fake_session_factory():
    return _SessionContext()


class FakeStore:
    """Returns fixed rows shaped like ``ChunkStore.search``/``search_lexical``."""

    def __init__(
        self,
        vector_rows: list[dict] | None = None,
        lexical_rows: list[dict] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.vector_rows = vector_rows if vector_rows is not None else default_vector_rows()
        self.lexical_rows = lexical_rows if lexical_rows is not None else default_lexical_rows()
        self.error = error
        self.vector_calls: list[dict] = []
        self.lexical_calls: list[dict] = []

    async def search(self, session, *, query_vector, k, distance_threshold=None):
        self.vector_calls.append(
            {
                "k": k,
                "vector": query_vector,
                "distance_threshold": distance_threshold,
            }
        )
        if self.error is not None:
            raise self.error
        return self.vector_rows[:k]

    async def search_lexical(self, session, *, query_text, top_k=50):
        self.lexical_calls.append({"query_text": query_text, "top_k": top_k})
        if self.error is not None:
            raise self.error
        return self.lexical_rows[:top_k]


class FakeReranker:
    """Records the call and keeps the first ``top_n`` candidates; can fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def rerank(self, query, candidates, *, top_n, text_of=lambda candidate: candidate.content):
        self.calls.append(
            {"query": query, "candidates": len(candidates), "top_n": top_n}
        )
        if self.fail:
            raise RuntimeError(
                "cross-encoder could not load /models/sealed-weights; "
                "api_key=sk-secret"
            )
        return list(candidates)[:top_n]


def default_vector_rows() -> list[dict]:
    """Three vector hits, ascending distance, full metadata."""
    return [
        vector_row(1, distance=0.10),
        vector_row(2, distance=0.20),
        vector_row(3, distance=0.30),
    ]


def default_lexical_rows() -> list[dict]:
    """Two lexical hits: chunk 2 (overlaps the vector branch) and chunk 4."""
    return [lexical_row(2, rank=0.50), lexical_row(4, rank=0.25)]


def vector_row(chunk_id: int, *, distance: float) -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": 100 + chunk_id,
        "chunk_type": "budget_component",
        "content": f"vector content for chunk {chunk_id}",
        "metadata": {
            "budget_id": f"BUD-2024-{chunk_id:03d}",
            "component_id": f"CMP-{chunk_id:03d}",
            "client_sector": "finance",
            "year": 2024,
        },
        "distance": distance,
    }


def lexical_row(chunk_id: int, *, rank: float) -> dict:
    """A lexical row carries ``rank`` and **no** distance at all."""
    return {
        "chunk_id": chunk_id,
        "document_id": 100 + chunk_id,
        "chunk_type": "budget_component",
        "content": f"lexical content for chunk {chunk_id}",
        "metadata": {
            "budget_id": f"BUD-2024-{chunk_id:03d}",
            "component_id": f"CMP-{chunk_id:03d}",
            "client_sector": "finance",
            "year": 2024,
        },
        "rank": rank,
    }


# --- wiring -----------------------------------------------------------------


@pytest.fixture
def wire(client, settings_holder):
    """Install fakes behind the router's dependency seam for one test."""

    def _wire(*, settings=None, embedder=_UNSET, store=None, reranker=_UNSET):
        if settings is not None:
            settings_holder.current = settings
        if embedder is _UNSET:
            embedder = FakeEmbedder()
        if reranker is _UNSET:
            reranker = FakeReranker()

        overrides = main.app.dependency_overrides
        overrides[get_retrieval_settings] = lambda: settings_holder.current
        overrides[get_embedder] = lambda: embedder
        overrides[get_chunk_store] = lambda: store
        overrides[get_reranker] = lambda: reranker
        overrides[get_retrieval_session_factory] = fake_session_factory
        return client

    yield _wire
    main.app.dependency_overrides.clear()


def post(client, payload: dict):
    return client.post(URL, json=payload)


def assert_matches_contract(body: dict) -> RetrievalResult:
    """Validate the body against the response model and pin the field set."""
    assert set(body) == RESULT_KEYS
    assert body["search_time_ms"] >= 0
    for chunk in body["chunks"]:
        assert set(chunk) == CHUNK_KEYS
    return RetrievalResult.model_validate(body)


# --- the four configurations ------------------------------------------------


def test_vector_without_rerank_returns_the_vector_ranking(wire) -> None:
    store = FakeStore()
    client = wire(store=store)

    response = post(
        client,
        {
            "query": "OAuth 2.0 authentication backend",
            "search_mode": "vector",
            "rerank": False,
            "top_k": 2,
            "distance_threshold": 0.4,
        },
    )

    assert response.status_code == 200
    result = assert_matches_contract(response.json())
    assert result.search_mode == "vector"
    assert result.rerank is False
    assert [chunk.chunk_id for chunk in result.chunks] == [1, 2]
    assert all(chunk.source == "vector" for chunk in result.chunks)
    assert result.candidates_evaluated == 2
    assert result.low_confidence is False
    # A plain vector search stops at top_k; nothing downstream reorders it.
    assert store.vector_calls == [
        {"k": 2, "vector": [0.01] * 8, "distance_threshold": 0.4}
    ]
    assert store.lexical_calls == []


def test_hybrid_without_rerank_fuses_both_branches(wire) -> None:
    store = FakeStore()
    client = wire(store=store)

    response = post(
        client,
        {
            "query": "OAuth 2.0 authentication backend",
            "search_mode": "hybrid",
            "rerank": False,
            "recall_k": 5,
            "top_k": 4,
        },
    )

    assert response.status_code == 200
    result = assert_matches_contract(response.json())
    assert result.search_mode == "hybrid"
    assert result.rerank is False
    assert store.vector_calls[0]["k"] == 5
    assert store.lexical_calls == [
        {"query_text": "OAuth 2.0 authentication backend", "top_k": 5}
    ]
    # Vector rows + lexical rows, counted before dedup (chunk 2 is in both).
    assert result.candidates_evaluated == 5
    assert len(result.chunks) == 4
    by_id = {chunk.chunk_id: chunk for chunk in result.chunks}
    # The lexical-only hit has no distance at all — never a sentinel value.
    assert by_id[4].source == "lexical"
    assert by_id[4].distance is None
    assert by_id[4].rank == 0.25


def test_vector_with_rerank_labels_survivors_fused(wire) -> None:
    store = FakeStore()
    reranker = FakeReranker()
    client = wire(store=store, reranker=reranker, settings=build_settings(RERANK_TOP_N=2))

    response = post(
        client,
        {
            "query": "OAuth 2.0 authentication backend",
            "search_mode": "vector",
            "rerank": True,
            "recall_k": 3,
            "top_k": 2,
        },
    )

    assert response.status_code == 200
    result = assert_matches_contract(response.json())
    assert result.search_mode == "vector"
    assert result.rerank is True
    assert store.lexical_calls == []
    assert store.vector_calls[0]["k"] == 3
    assert reranker.calls == [
        {"query": "OAuth 2.0 authentication backend", "candidates": 3, "top_n": 2}
    ]
    assert [chunk.chunk_id for chunk in result.chunks] == [1, 2]
    # No fusion ran in vector mode, so no chunk may claim "fused". Reranking
    # reorders; it does not rewrite provenance.
    assert all(chunk.source == "vector" for chunk in result.chunks)


def test_hybrid_with_rerank_fuses_then_reranks(wire) -> None:
    store = FakeStore()
    reranker = FakeReranker()
    client = wire(store=store, reranker=reranker, settings=build_settings(RERANK_TOP_N=3))

    response = post(
        client,
        {
            "query": "OAuth 2.0 authentication backend",
            "search_mode": "hybrid",
            "rerank": True,
            "recall_k": 5,
            "top_k": 3,
        },
    )

    assert response.status_code == 200
    result = assert_matches_contract(response.json())
    assert result.rerank is True
    assert store.lexical_calls != []
    assert result.candidates_evaluated == 5
    # 3 vector ids + 1 lexical-only id (chunk 2 is in both branches).
    assert reranker.calls == [
        {"query": "OAuth 2.0 authentication backend", "candidates": 4, "top_n": 3}
    ]
    by_id = {chunk.chunk_id: chunk for chunk in result.chunks}
    # Provenance survives reranking: only the id both branches returned is
    # "fused"; a vector-only hit stays "vector", a lexical-only hit "lexical".
    assert by_id[2].source == "fused"
    assert {c.source for c in result.chunks} <= {"vector", "lexical", "fused"}
    assert any(c.source != "fused" for c in result.chunks)


def test_empty_corpus_is_a_200_with_low_confidence(wire) -> None:
    client = wire(store=FakeStore(vector_rows=[], lexical_rows=[]))

    response = post(client, {"query": "anything", "search_mode": "hybrid"})

    assert response.status_code == 200
    result = assert_matches_contract(response.json())
    assert result.chunks == []
    assert result.candidates_evaluated == 0
    assert result.low_confidence is True


# --- defaults from Settings -------------------------------------------------


def test_omitted_fields_take_their_defaults_from_settings(wire) -> None:
    store = FakeStore()
    reranker = FakeReranker()
    settings = build_settings(
        RETRIEVAL_SEARCH_MODE="hybrid",
        RERANKER_ENABLED=True,
        RRF_K=17,
        RETRIEVAL_RECALL_TOP_K=7,
        RERANK_TOP_N=3,
    )
    client = wire(store=store, reranker=reranker, settings=settings)

    response = post(client, {"query": "OAuth 2.0 authentication backend"})

    assert response.status_code == 200
    result = assert_matches_contract(response.json())
    assert result.search_mode == settings.RETRIEVAL_SEARCH_MODE
    assert result.rerank is settings.RERANKER_ENABLED
    # recall_k is omitted, so the deployment default widens both branches.
    assert store.vector_calls[0]["k"] == settings.RETRIEVAL_RECALL_TOP_K
    assert store.lexical_calls[0]["top_k"] == settings.RETRIEVAL_RECALL_TOP_K
    assert reranker.calls[0]["top_n"] == settings.RERANK_TOP_N


def test_explicit_request_fields_win_over_settings(wire) -> None:
    store = FakeStore()
    reranker = FakeReranker()
    settings = build_settings(
        RETRIEVAL_SEARCH_MODE="hybrid",
        RERANKER_ENABLED=True,
        RETRIEVAL_RECALL_TOP_K=7,
    )
    client = wire(store=store, reranker=reranker, settings=settings)

    response = post(
        client,
        {"query": "OAuth 2.0 authentication backend", "search_mode": "vector", "rerank": False},
    )

    assert response.status_code == 200
    result = assert_matches_contract(response.json())
    assert result.search_mode == "vector"
    assert result.rerank is False
    assert store.lexical_calls == []
    assert reranker.calls == []
    assert all(chunk.source == "vector" for chunk in result.chunks)


# --- 422, before any I/O ----------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"query": "x", "rrf_k": 0}, id="rrf_k_zero"),
        pytest.param({"query": "x", "rrf_k": 201}, id="rrf_k_too_large"),
        pytest.param({"query": "x", "search_mode": "semantic"}, id="unknown_search_mode"),
        pytest.param({"query": "x", "top_k": MAX_SEARCH_K + 1}, id="top_k_too_large"),
        pytest.param({"query": "x", "top_k": 0}, id="top_k_zero"),
        pytest.param({"query": "x", "recall_k": 201}, id="recall_k_too_large"),
        pytest.param({"query": "x", "distance_threshold": 2.5}, id="threshold_too_large"),
        pytest.param({"query": ""}, id="empty_query"),
        pytest.param({"query": "x" * 20_001}, id="query_too_long"),
        pytest.param({"query": "x", "extra": 1}, id="unknown_field"),
        pytest.param({}, id="missing_query"),
    ],
)
def test_invalid_body_is_a_422_before_any_work(wire, payload: dict) -> None:
    embedder = FakeEmbedder()
    store = FakeStore()
    client = wire(embedder=embedder, store=store)

    response = post(client, payload)

    assert response.status_code == 422
    assert embedder.calls == []
    assert store.vector_calls == []
    assert store.lexical_calls == []


# --- the failure branches ---------------------------------------------------


def test_reranker_failure_is_a_502_with_the_documented_body(wire) -> None:
    client = wire(store=FakeStore(), reranker=FakeReranker(fail=True))

    response = post(client, {"query": "anything", "rerank": True})

    assert response.status_code == 502
    assert response.json() == {"detail": RERANKER_ERROR}
    # Never leak the exception text, the model path or a key.
    assert "sealed-weights" not in response.text
    assert "sk-secret" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("search_mode", ["vector", "hybrid"])
def test_a_broken_reranker_does_not_affect_unreranked_paths(wire, search_mode: str) -> None:
    """The same failing reranker is wired; ``rerank=false`` must still be a 200."""
    reranker = FakeReranker(fail=True)
    client = wire(store=FakeStore(), reranker=reranker)

    response = post(client, {"query": "anything", "rerank": False, "search_mode": search_mode})

    assert response.status_code == 200
    assert response.json()["rerank"] is False
    assert reranker.calls == []


def test_missing_embedder_is_a_500(wire) -> None:
    client = wire(embedder=None, store=FakeStore())

    response = post(client, {"query": "anything"})

    assert response.status_code == 500
    assert response.json() == {"detail": EMBEDDER_ERROR}


def test_unexpected_failure_is_a_generic_500_without_leaking_detail(wire) -> None:
    store = FakeStore(error=RuntimeError("sk-secret database dsn leaked here"))
    client = wire(store=store)

    response = post(client, {"query": "anything"})

    assert response.status_code == 500
    assert response.json() == {"detail": GENERIC_ERROR}
    assert "sk-secret" not in response.text
    assert "database dsn" not in response.text


# --- contract visibility ----------------------------------------------------


def test_the_new_route_is_documented_in_openapi(client) -> None:
    spec = client.get("/openapi.json").json()

    assert URL in spec["paths"]
    operation = spec["paths"][URL]["post"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema.get("$ref", "").endswith("RetrievalResult")
