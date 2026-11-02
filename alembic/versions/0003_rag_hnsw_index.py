"""Session 9 — HNSW index on ``chunks.embedding``.

Revision ID: 0003_rag_hnsw_index
Revises: 0002_rag_pgvector
Create Date: 2026-11-02

The Session-8 migration deliberately created no vector index so the sequential
scan could serve as the baseline. This revision is that baseline's successor:
an approximate-nearest-neighbour index over cosine distance.

* ``vector_cosine_ops`` — must match the ``<=>`` operator emitted by
  ``ChunkRow.embedding.cosine_distance``; any other operator class makes the
  planner ignore the index and silently fall back to a sequential scan.
* ``m = 16`` / ``ef_construction = 128`` — graph degree and build-time candidate
  list. They are baked into the graph: changing them means DROP + CREATE (a
  full reindex), which is why they are literals here rather than settings.
* ``ef_search`` is *query* time (default 40 in ``ChunkStore.search`` via
  ``SET LOCAL``), so it is not part of the index definition.

**Postgres only, guarded by dialect**, same as 0002: the host ``alembic upgrade
head`` runs against SQLite, where ``USING hnsw`` does not exist.

Concurrently is intentionally NOT used: ``CREATE INDEX CONCURRENTLY`` cannot run
inside Alembic's transaction, and the corpus is small enough that a plain build
completes immediately.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_rag_hnsw_index"
down_revision: Union[str, None] = "0002_rag_pgvector"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Mirrors ``store.models.HNSW_M`` / ``HNSW_EF_CONSTRUCTION``. Repeated rather
#: than imported because a migration is frozen history: a later change to the
#: module constant must not silently rewrite what this revision did.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 128
INDEX_NAME = "ix_chunks_embedding_hnsw"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX_NAME}
        ON chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
