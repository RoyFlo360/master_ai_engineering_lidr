"""Unit tests for the transactional ingest service.

No database, no provider: the store and the embedder are fakes, the chunker is
the real one. These pin the properties the one-transaction design exists to
guarantee — order of operations, the duplicate guard, and that a failed embedding
call never reaches the store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.ingest_service import (
    DuplicateDocumentError,
    EmbeddingDimensionMismatch,
    RagIngestService,
)
from app.embedding_pipeline.schemas import Budget, Chunk, EmbeddedChunk
from app.embedding_pipeline.store.models import EMBEDDING_DIMENSIONS

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


def one_budget(budget_id: str = "BUD-2024-001") -> Budget:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[0]
    return Budget.model_validate({**payload, "budget_id": budget_id})


# --- fakes -------------------------------------------------------------------


class FakeTransaction:
    def __init__(self, session: "FakeSession") -> None:
        self._session = session

    async def __aenter__(self) -> "FakeTransaction":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._session.committed = exc_type is None
        self._session.rolled_back = exc_type is not None
        return False


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeSessionFactory:
    """Mimics ``async_sessionmaker``: call it, use the result as a context manager."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


class FakeStore:
    def __init__(self, *, existing_id: int | None = None, dimension: int = EMBEDDING_DIMENSIONS):
        self.existing_id = existing_id
        self.dimension = dimension
        self.persisted: list[dict] = []

    async def find_document_id(self, session, source_path: str) -> int | None:
        return self.existing_id

    async def persist_document_with_chunks(
        self, session, *, source_path, document_type, doc_metadata, embedded_chunks
    ) -> int:
        self.persisted.append(
            {
                "source_path": source_path,
                "document_type": document_type,
                "doc_metadata": doc_metadata,
                "chunks": list(embedded_chunks),
            }
        )
        return 1


class FakeEmbedder:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.seen: list[list[Chunk]] = []

    def embed_many(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        self.seen.append(list(chunks))
        if self.error is not None:
            raise self.error
        return [
            EmbeddedChunk(**chunk.model_dump(), embedding=[0.1] * EMBEDDING_DIMENSIONS)
            for chunk in chunks
        ]


def build_service(*, store: FakeStore, embedder: FakeEmbedder):
    factory = FakeSessionFactory()
    service = RagIngestService(
        chunker=JSONStructuralChunker(),
        embedder=embedder,
        session_factory=factory,
        store=store,
    )
    return service, factory


# --- tests -------------------------------------------------------------------


def test_ingest_persists_every_component_chunk() -> None:
    import asyncio

    budget = one_budget()
    store = FakeStore()
    embedder = FakeEmbedder()
    service, factory = build_service(store=store, embedder=embedder)

    response = asyncio.run(
        service.ingest(
            source_path="sample::BUD-2024-001",
            document_type="historical_budget",
            budget=budget,
        )
    )

    components = len(budget.components)
    assert response.chunks_created == components
    assert response.embedding_dimension == EMBEDDING_DIMENSIONS
    assert response.document_id == 1
    assert response.ingestion_time_ms >= 0
    assert len(store.persisted) == 1
    assert len(store.persisted[0]["chunks"]) == components
    # Document metadata is what a future filter would query.
    assert store.persisted[0]["doc_metadata"] == {
        "budget_id": "BUD-2024-001",
        "client_sector": budget.client_metadata.sector,
        "year": budget.year,
    }
    assert factory.sessions[0].committed is True
    assert factory.sessions[0].rolled_back is False


def test_ingest_embeds_once_for_the_whole_document() -> None:
    """Batching is the embedder's job; the service must not call it per chunk."""
    import asyncio

    store = FakeStore()
    embedder = FakeEmbedder()
    service, _ = build_service(store=store, embedder=embedder)

    asyncio.run(
        service.ingest(
            source_path="sample::BUD-2024-001",
            document_type="historical_budget",
            budget=one_budget(),
        )
    )

    assert len(embedder.seen) == 1
    assert len(embedder.seen[0]) == len(one_budget().components)


def test_duplicate_source_path_raises_before_any_work() -> None:
    import asyncio

    store = FakeStore(existing_id=42)
    embedder = FakeEmbedder()
    service, factory = build_service(store=store, embedder=embedder)

    with pytest.raises(DuplicateDocumentError) as excinfo:
        asyncio.run(
            service.ingest(
                source_path="sample::BUD-2024-001",
                document_type="historical_budget",
                budget=one_budget(),
            )
        )

    assert excinfo.value.document_id == 42
    # Nothing was embedded and nothing was written.
    assert embedder.seen == []
    assert store.persisted == []
    assert factory.sessions[0].rolled_back is True


def test_embedder_failure_leaves_no_document_and_rolls_back() -> None:
    """The property the single transaction exists for."""
    import asyncio

    store = FakeStore()
    embedder = FakeEmbedder(error=RuntimeError("provider down"))
    service, factory = build_service(store=store, embedder=embedder)

    with pytest.raises(RuntimeError, match="provider down"):
        asyncio.run(
            service.ingest(
                source_path="sample::BUD-2024-001",
                document_type="historical_budget",
                budget=one_budget(),
            )
        )

    assert store.persisted == []
    assert factory.sessions[0].rolled_back is True
    assert factory.sessions[0].committed is False


def test_wrong_dimension_is_refused_before_persisting(monkeypatch) -> None:
    """A 768-dim vector in a vector(1536) column would poison every later search."""
    import asyncio

    store = FakeStore()
    service, factory = build_service(store=store, embedder=FakeEmbedder())

    def short_vectors(chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [EmbeddedChunk(**chunk.model_dump(), embedding=[0.1] * 768) for chunk in chunks]

    monkeypatch.setattr(service._embedder, "embed_many", short_vectors)

    with pytest.raises(EmbeddingDimensionMismatch):
        asyncio.run(
            service.ingest(
                source_path="sample::BUD-2024-001",
                document_type="historical_budget",
                budget=one_budget(),
            )
        )

    assert store.persisted == []
    assert factory.sessions[0].rolled_back is True
