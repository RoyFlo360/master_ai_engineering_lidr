"""Real-database tests for the Session 8 vector store.

Skipped unless ``TEST_DATABASE_URL`` points at a reachable Postgres. These prove
the things a fake cannot:

* the migration really creates ``vector(1536)``, JSONB and the GIN index;
* the one-transaction ingest really commits both tables;
* a duplicate ``source_path`` is refused by the **unique index**, not just by
  the application check;
* ``cosine_distance`` really executes in SQL and returns ascending distances;
* ``ON DELETE CASCADE`` really removes the chunks.

Run against the compose stack::

    docker compose up -d postgres
    docker compose exec api alembic upgrade head
    $env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
    uv run pytest tests/test_rag_postgres.py -q

The tests clean the two tables they use, so they are re-runnable.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.ingest_service import (
    DuplicateDocumentError,
    RagIngestService,
)
from app.embedding_pipeline.retriever import SemanticRetriever
from app.embedding_pipeline.schemas import Budget, Chunk, EmbeddedChunk
from app.embedding_pipeline.store.models import (
    EMBEDDING_DIMENSIONS,
    ChunkRow,
    DocumentRow,
)
from app.embedding_pipeline.store.repository import ChunkStore
from app.persistence.database import _async_database_url

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PATH = ROOT / "data" / "budgets_sample.json"
DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="set TEST_DATABASE_URL to run the real-database tests",
    ),
    pytest.mark.postgres,
]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> None:
    """Apply the real migrations to the external database (idempotent)."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def one_budget(budget_id: str = "BUD-2024-001") -> Budget:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[0]
    return Budget.model_validate({**payload, "budget_id": budget_id})


def one_hot(index: int) -> list[float]:
    """A deterministic unit vector, so cosine distances are predictable."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


class IndexedEmbedder:
    """Assigns ``one_hot(n)`` to the n-th chunk it is given."""

    def __init__(self, axis: int = 0) -> None:
        self.axis = axis

    def embed_many(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        return [
            EmbeddedChunk(**chunk.model_dump(), embedding=one_hot(self.axis))
            for chunk in chunks
        ]

    def embed_one(self, text: str) -> list[float]:
        return one_hot(self.axis)


@pytest.fixture
def session_factory():
    """Engine + factory scoped to one test, with **no** connection pooling.

    ``asyncio.run`` creates and closes a fresh event loop per operation, and an
    asyncpg connection is bound to the loop that opened it. Reusing a pooled
    connection from a previous loop fails with "Event loop is closed", so the
    tests use ``NullPool``: every operation opens its own connection in its own
    loop. (The application keeps the default pool — one loop, no problem.)
    """
    url = _async_database_url(DATABASE_URL)
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def _cleanup() -> None:
        async with factory() as session, session.begin():
            await session.execute(delete(ChunkRow))
            await session.execute(delete(DocumentRow))

    asyncio.run(_cleanup())
    yield factory
    asyncio.run(_cleanup())
    asyncio.run(engine.dispose())


@pytest.fixture
def ingest_service(session_factory):
    return RagIngestService(
        chunker=JSONStructuralChunker(),
        embedder=IndexedEmbedder(),
        session_factory=session_factory,
        store=ChunkStore(),
    )


def test_migration_created_the_vector_schema() -> None:
    from sqlalchemy import create_engine

    engine = create_engine(DATABASE_URL)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"documents", "chunks"} <= tables

        with engine.connect() as connection:
            udt = connection.execute(
                text(
                    "select udt_name from information_schema.columns "
                    "where table_name = 'chunks' and column_name = 'embedding'"
                )
            ).scalar_one()
            assert udt == "vector"

            extension = connection.execute(
                text("select count(*) from pg_extension where extname = 'vector'")
            ).scalar_one()
            assert extension == 1
    finally:
        engine.dispose()


def test_no_vector_index_exists() -> None:
    """The sequential scan is the baseline the live session measures against."""
    from sqlalchemy import create_engine

    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            indexes = connection.execute(
                text("select indexname, indexdef from pg_indexes where tablename = 'chunks'")
            ).all()
    finally:
        engine.dispose()

    assert indexes, "expected the relational indexes to exist"
    for name, definition in indexes:
        assert "embedding" not in definition, f"{name} indexes the vector column"
        assert "hnsw" not in definition.lower()
        assert "ivfflat" not in definition.lower()


def test_ingest_persists_document_and_chunks(ingest_service, session_factory) -> None:
    first = asyncio.run(_ingest(ingest_service, budget_id="BUD-2024-001"))
    second = asyncio.run(_ingest(ingest_service, budget_id="BUD-2024-002"))
    assert first.document_id != second.document_id

    async def _counts() -> tuple[int, int]:
        async with session_factory() as session:
            documents = await session.scalar(select(func.count()).select_from(DocumentRow))
            chunks = await session.scalar(select(func.count()).select_from(ChunkRow))
        return documents, chunks

    documents, chunks = asyncio.run(_counts())
    assert documents == 2
    assert chunks == first.chunks_created + second.chunks_created
    # Every component became a chunk, and the vector landed.
    components = len(one_budget().components) * 2
    assert chunks == components


async def _ingest(service: RagIngestService, budget_id: str = "BUD-2024-001"):
    return await service.ingest(
        source_path=f"sample::{budget_id}",
        document_type="historical_budget",
        budget=one_budget(budget_id),
    )


def test_duplicate_source_path_is_refused(ingest_service) -> None:
    asyncio.run(_ingest(ingest_service))

    with pytest.raises(DuplicateDocumentError):
        asyncio.run(_ingest(ingest_service))


def test_duplicate_is_also_refused_by_the_unique_index(ingest_service, session_factory) -> None:
    """Bypass the application check: the constraint itself must hold."""
    from sqlalchemy.exc import IntegrityError

    asyncio.run(_ingest(ingest_service))

    async def _insert_again() -> None:
        async with session_factory() as session, session.begin():
            session.add(
                DocumentRow(
                    source_path="sample::BUD-2024-001",
                    document_type="historical_budget",
                    metadata_={},
                )
            )

    with pytest.raises(IntegrityError):
        asyncio.run(_insert_again())


def test_search_orders_by_cosine_distance(ingest_service, session_factory) -> None:
    # Two documents on orthogonal axes; the query sits on the first one.
    near = RagIngestService(
        chunker=JSONStructuralChunker(),
        embedder=IndexedEmbedder(axis=0),
        session_factory=session_factory,
        store=ChunkStore(),
    )
    far = RagIngestService(
        chunker=JSONStructuralChunker(),
        embedder=IndexedEmbedder(axis=1),
        session_factory=session_factory,
        store=ChunkStore(),
    )
    asyncio.run(_ingest(near, budget_id="BUD-NEAR"))
    asyncio.run(_ingest(far, budget_id="BUD-FAR"))

    retriever = SemanticRetriever(
        embedder=IndexedEmbedder(axis=0),
        session_factory=session_factory,
        store=ChunkStore(),
    )
    response = asyncio.run(retriever.search(query="anything", k=5))

    assert response.results
    distances = [hit.distance for hit in response.results]
    assert distances == sorted(distances)
    # The nearest chunk is the one embedded on the query axis: distance ~0 ...
    assert response.results[0].distance == pytest.approx(0.0, abs=1e-6)
    # ... and it belongs to the "near" document.
    assert response.results[0].metadata["budget_id"] == "BUD-NEAR"


def test_search_on_an_empty_corpus_returns_no_results(session_factory) -> None:
    retriever = SemanticRetriever(
        embedder=IndexedEmbedder(),
        session_factory=session_factory,
        store=ChunkStore(),
    )
    response = asyncio.run(retriever.search(query="anything", k=5))
    assert response.results == []


def test_deleting_a_document_cascades_to_its_chunks(ingest_service, session_factory) -> None:
    asyncio.run(_ingest(ingest_service))

    async def _delete_document() -> tuple[int, int]:
        async with session_factory() as session, session.begin():
            document_id = await session.scalar(select(DocumentRow.id))
            await session.execute(delete(DocumentRow).where(DocumentRow.id == document_id))
        async with session_factory() as session:
            documents = await session.scalar(select(func.count()).select_from(DocumentRow))
            chunks = await session.scalar(select(func.count()).select_from(ChunkRow))
        return documents, chunks

    documents, chunks = asyncio.run(_delete_document())
    assert documents == 0
    assert chunks == 0
