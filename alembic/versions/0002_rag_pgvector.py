"""Session 8 — pgvector extension + documents/chunks tables.

Revision ID: 0002_rag_pgvector
Revises: 0001_ingestion
Create Date: 2026-10-24

Deliberately NO vector index (HNSW / IVFFlat): the sequential scan is the
baseline the live session measures the index impact against. The non-vector
indexes (FK, chunk_type, GIN on metadata) ARE created here because they are
plain relational hygiene, not the subject of the live demo.

``ix_documents_source_path`` is **unique** (a deliberate strengthening of the
exercise's plain index): the application-level duplicate check produces the
friendly 409, and the constraint makes "one document per source_path" true under
concurrent identical ingests.

**Postgres only, guarded by dialect.** ``CREATE EXTENSION``, ``JSONB`` and
``vector`` do not exist on SQLite, but ``alembic upgrade head`` runs on the host
against ``var/estimator.db`` as well as in compose against Postgres. Returning
early on a non-Postgres connection keeps the host path working; the RAG
endpoints fail with an explicit "not available" message instead.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002_rag_pgvector"
down_revision: Union[str, None] = "0001_ingestion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: text-embedding-3-small. Mirrors ``store.models.EMBEDDING_DIMENSIONS``; the
#: migration is frozen history, so it repeats the literal rather than importing
#: a value that could later change.
EMBEDDING_DIMENSIONS = 1536


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # Must run before any Vector column exists. The pgvector/pgvector:pg16 image
    # ships the extension compiled; this single statement activates it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source_path", sa.Text, nullable=False),
        sa.Column("document_type", sa.String(length=50), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_documents_source_path", "documents", ["source_path"], unique=True
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.BigInteger,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_type", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        # Nullable: leaves the door open to insert-now-embed-later ingestion
        # (future sessions). Session 8 always writes chunk+vector atomically.
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_chunk_type", "chunks", ["chunk_type"])
    # GIN over the whole JSONB: query arbitrary metadata keys without a schema
    # migration per new key.
    op.create_index("ix_chunks_metadata_gin", "chunks", ["metadata"], postgresql_using="gin")


def downgrade() -> None:
    if not _is_postgres():
        return

    op.drop_index("ix_chunks_metadata_gin", table_name="chunks")
    op.drop_index("ix_chunks_chunk_type", table_name="chunks")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_documents_source_path", table_name="documents")
    op.drop_table("documents")
    # The vector extension is left installed: dropping an extension is a
    # cluster-level operation that could break other objects, and re-creating it
    # is free (IF NOT EXISTS) on the next upgrade.
