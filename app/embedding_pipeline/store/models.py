"""SQLAlchemy ORM models for the vector store (Session 8).

Two tables, a real one-to-many:

* ``documents`` — one row per ingested source (a historical budget). Owns
  provenance: where it came from, when, and document-level metadata.
* ``chunks`` — N rows per document, each carrying the embeddable text and its
  1536-dim vector. ``ON DELETE CASCADE`` means deleting a budget removes all its
  chunks: referential integrity instead of denormalized duplication.

Design notes (justified in the README and in spec 012):

* ``metadata`` is a JSONB column on both tables. Stable fields live in typed
  columns; whatever the chunker enriches (sector, technologies, hours) goes to
  JSONB, queryable via the GIN index without a migration per new key.
* ``embedding`` is **nullable**: it allows inserting a chunk first and filling
  the vector later (async ingestion, future sessions). Session 8 ingests
  chunk+embedding atomically and never exercises that path.
* ``Vector(1536)`` is hard-coded to ``text-embedding-3-small``'s dimensionality;
  changing it means re-embedding the whole corpus, so it is not configuration.
  ``EMBEDDING_DIMENSIONS`` is exported so the embedder, the migration and the
  startup guard all read the same number.
* **No vector index on purpose** — the live session adds HNSW and measures the
  before/after against this sequential-scan baseline.

**A separate declarative base, not the shared one.** These tables use
``Vector``, ``JSONB`` and a Postgres-only extension. ``app.persistence.models``
must keep compiling on SQLite because ``tests/test_ingestion_persistence.py``
builds a temp database with ``Base.metadata.create_all()``; putting a vector
column there would break ten passing tests for an unrelated reason.

``metadata`` is a reserved attribute on SQLAlchemy declarative models, so the
Python attribute is ``metadata_`` mapped onto the ``"metadata"`` column.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

#: Dimensionality of ``text-embedding-3-small``. One number, three consumers:
#: the ORM column, the migration and the startup guard.
EMBEDDING_DIMENSIONS = 1536


class RagBase(DeclarativeBase):
    """Declarative base for the Postgres-only vector store.

    Deliberately separate from ``app.persistence.models.Base``: the ingestion
    models must stay portable to SQLite, and these cannot be.
    """


class DocumentRow(RagBase):
    __tablename__ = "documents"
    __table_args__ = (
        # Unique *and* indexed: the unique constraint is what makes "one document
        # per source_path" true under concurrency; the application-level check in
        # the ingest service only produces the friendly 409.
        Index("ix_documents_source_path", "source_path", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(String(50), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    chunks: Mapped[list[ChunkRow]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class ChunkRow(RagBase):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_chunk_type", "chunk_type"),
        Index("ix_chunks_metadata_gin", "metadata", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[DocumentRow] = relationship(back_populates="chunks")
