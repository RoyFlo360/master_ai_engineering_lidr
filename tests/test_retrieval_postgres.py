"""Real-database tests for the Session-10 lexical branch (T5.2).

Skipped unless ``TEST_DATABASE_URL`` points at a reachable Postgres. These prove
the things a fake cannot:

* migration ``0004_rag_fts`` really creates ``chunks.content_tsv`` as a
  ``tsvector`` with a GIN index;
* ``ChunkStore.search_lexical`` really finds a rare identifier that the vector
  branch never returns, and really returns nothing for a stop-word-only query
  (``plainto_tsquery`` collapses it to an empty tsquery);
* the two branches can disagree — the lexical branch surfaces a chunk the vector
  branch's top-k does not contain, which is the whole reason hybrid search
  exists;
* a migration round-trip (``upgrade head`` → ``downgrade -1`` → ``upgrade head``)
  drops and restores ``content_tsv`` while ``ix_chunks_embedding_hnsw`` survives
  untouched.

Run against the compose stack::

    docker compose up -d postgres
    docker compose exec api alembic upgrade head
    $env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
    uv run pytest tests/test_retrieval_postgres.py -q

The tests clean only the rows they create (every document they insert carries the
``s10::lexical::`` ``source_path`` prefix), so pointing ``TEST_DATABASE_URL`` at a
database that already holds the corpus is safe — it is not wiped. The round-trip
test mutates the schema and restores it in a ``finally`` block even when an
assertion fails.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.embedding_pipeline.store.models import (
    EMBEDDING_DIMENSIONS,
    ChunkRow,
    DocumentRow,
)
from app.embedding_pipeline.store.repository import ChunkStore
from app.persistence.database import _async_database_url

ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="set TEST_DATABASE_URL to run the real-database tests",
    ),
    pytest.mark.postgres,
]

#: A token no other test fixture, sample budget or seed file contains, chosen so
#: the lexical assertion cannot pass because of pre-existing corpus rows.
RARE_IDENTIFIER = "ZQX-9917"

#: ``source_path`` prefix stamped on every document this module creates. Cleanup
#: deletes by this prefix, never by table, so running these tests against a
#: database that already holds the corpus leaves it intact.
OWNED_PREFIX = "s10::lexical::"

#: Text made only of English stop-words: ``plainto_tsquery('english', …)``
#: produces an empty tsquery, which matches no row at all.
STOP_WORDS = "the and of to with"


def alembic_config() -> Config:
    """The repository's Alembic config pointed at the external database."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="module", autouse=True)
def migrated() -> None:
    """Apply the real migrations to the external database (idempotent)."""
    command.upgrade(alembic_config(), "head")


def one_hot(index: int) -> list[float]:
    """A deterministic unit vector, so cosine distances are predictable."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


@pytest.fixture
def session_factory():
    """Engine + factory scoped to one test, with **no** connection pooling.

    Same reasoning as ``tests/test_rag_postgres.py``: ``asyncio.run`` opens a
    fresh event loop per operation, and an asyncpg connection is bound to the
    loop that opened it. ``NullPool`` gives every operation its own connection.
    """
    engine = create_async_engine(_async_database_url(DATABASE_URL), poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def _cleanup() -> None:
        # Only the rows this module created. A blanket ``DELETE FROM chunks``
        # would wipe whatever corpus the database already holds — and since
        # ``TEST_DATABASE_URL`` is commonly pointed at the compose database,
        # that means silently destroying the ingested budgets.
        async with factory() as session, session.begin():
            owned = select(DocumentRow.id).where(
                DocumentRow.source_path.like(f"{OWNED_PREFIX}%")
            )
            await session.execute(delete(ChunkRow).where(ChunkRow.document_id.in_(owned)))
            await session.execute(
                delete(DocumentRow).where(DocumentRow.source_path.like(f"{OWNED_PREFIX}%"))
            )

    asyncio.run(_cleanup())
    yield factory
    asyncio.run(_cleanup())
    asyncio.run(engine.dispose())


def seed_chunks(session_factory, chunks: list[tuple[str, list[float]]]) -> list[int]:
    """Insert one document with the given ``(content, embedding)`` chunks.

    Returns the chunk ids in insertion order, so a test can name the row it
    expects from each branch.
    """
    ids: list[int] = []

    async def _insert() -> None:
        async with session_factory() as session, session.begin():
            document = DocumentRow(
                source_path=f"{OWNED_PREFIX}{RARE_IDENTIFIER}",
                document_type="historical_budget",
                metadata_={"budget_id": "BUD-S10-LEX"},
            )
            session.add(document)
            await session.flush()
            rows = [
                ChunkRow(
                    document_id=document.id,
                    chunk_type="budget_component",
                    content=content,
                    embedding=embedding,
                    metadata_={"budget_id": "BUD-S10-LEX"},
                )
                for content, embedding in chunks
            ]
            session.add_all(rows)
            await session.flush()
            ids.extend(row.id for row in rows)

    asyncio.run(_insert())
    return ids


def lexical_ids(session_factory, query_text: str, *, top_k: int = 5) -> list[int]:
    """``ChunkStore.search_lexical`` against the real database, ids only."""

    async def _search() -> list[int]:
        async with session_factory() as session:
            rows = await ChunkStore().search_lexical(
                session, query_text=query_text, top_k=top_k
            )
        return [row.chunk_id for row in rows]

    return asyncio.run(_search())


def vector_ids(session_factory, query_vector: list[float], *, k: int = 5) -> list[int]:
    """``ChunkStore.search`` against the real database, ids only."""

    async def _search() -> list[int]:
        async with session_factory() as session:
            rows = await ChunkStore().search(session, query_vector=query_vector, k=k)
        return [row.chunk_id for row in rows]

    return asyncio.run(_search())


def schema_snapshot() -> tuple[bool, set[str]]:
    """Return ``(content_tsv exists, index names on chunks)`` from the catalog."""
    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            column_type = connection.execute(
                text(
                    "select udt_name from information_schema.columns "
                    "where table_name = 'chunks' and column_name = 'content_tsv'"
                )
            ).scalar_one_or_none()
            indexes = {
                row[0]
                for row in connection.execute(
                    text("select indexname from pg_indexes where tablename = 'chunks'")
                ).all()
            }
    finally:
        engine.dispose()
    return (column_type == "tsvector", indexes)


def test_search_lexical_finds_a_rare_identifier(session_factory) -> None:
    rare_id, _ = seed_chunks(
        session_factory,
        [
            (f"Delivery of the {RARE_IDENTIFIER} telemetry gateway phase", one_hot(1)),
            (
                "Mobile banking API with OAuth 2.0 authentication and PSD2 compliance",
                one_hot(2),
            ),
        ],
    )

    found = lexical_ids(session_factory, RARE_IDENTIFIER, top_k=5)

    assert found, f"search_lexical returned nothing for {RARE_IDENTIFIER!r}"
    assert found[0] == rare_id
    # Proves the hit is not an artifact of "the table has one row": the generic
    # chunk must not match a query whose tokens it does not contain.
    assert len(found) == 1


def test_search_lexical_returns_nothing_for_a_stop_word_only_query(session_factory) -> None:
    seed_chunks(
        session_factory,
        [
            ("the architecture of the system and the rollout of the platform", one_hot(3)),
            ("the and of to with", one_hot(4)),
        ],
    )

    assert lexical_ids(session_factory, STOP_WORDS, top_k=5) == []


def test_lexical_branch_surfaces_a_row_the_vector_top_k_misses(session_factory) -> None:
    """The branches can disagree — the premise of hybrid search.

    The rare-token chunk is embedded on an axis orthogonal to the query, so the
    vector branch's top-1 is the generic chunk. The lexical branch sees only the
    rare token, so it returns the chunk the vector branch dropped. If the two
    lists were ever identical this test fails: one branch would add nothing.
    """
    rare_id, generic_id = seed_chunks(
        session_factory,
        [
            (f"Delivery of the {RARE_IDENTIFIER} telemetry gateway phase", one_hot(1)),
            ("Mobile banking API with OAuth 2.0 authentication", one_hot(0)),
        ],
    )
    query_vector = one_hot(0)

    nearest = vector_ids(session_factory, query_vector, k=1)
    lexical = lexical_ids(session_factory, RARE_IDENTIFIER, top_k=5)

    assert nearest == [generic_id]
    assert lexical == [rare_id]
    assert nearest != lexical
    assert set(nearest).isdisjoint(lexical)


def test_migration_created_content_tsv_with_a_gin_index() -> None:
    has_column, indexes = schema_snapshot()

    assert has_column, "chunks.content_tsv is missing after alembic upgrade head"
    assert "ix_chunks_content_tsv" in indexes

    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            definition = connection.execute(
                text(
                    "select indexdef from pg_indexes "
                    "where tablename = 'chunks' and indexname = 'ix_chunks_content_tsv'"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    definition = definition.lower()
    assert "using gin" in definition
    assert "content_tsv" in definition


def test_migration_round_trip_drops_and_restores_content_tsv() -> None:
    """``upgrade head`` → ``downgrade -1`` → ``upgrade head``.

    ``content_tsv`` is a generated column, so it cannot be altered in place; the
    downgrade must drop it and the upgrade must add it back. The HNSW index is
    owned by ``0003`` and must survive both directions — a migration that
    recreates the ``chunks`` table instead of altering it would destroy it.
    """
    config = alembic_config()
    command.upgrade(config, "head")
    try:
        command.downgrade(config, "-1")
        has_column, indexes = schema_snapshot()
        assert not has_column, "downgrade left chunks.content_tsv behind"
        assert "ix_chunks_content_tsv" not in indexes
        assert "ix_chunks_embedding_hnsw" in indexes, "downgrade destroyed the HNSW index"
    finally:
        command.upgrade(config, "head")

    has_column, indexes = schema_snapshot()
    assert has_column, "upgrade did not restore chunks.content_tsv"
    assert "ix_chunks_content_tsv" in indexes
    assert "ix_chunks_embedding_hnsw" in indexes
