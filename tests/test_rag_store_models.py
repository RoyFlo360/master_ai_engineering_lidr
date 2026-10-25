"""Schema-level tests for the Session 8 ORM models.

Pure metadata introspection — no engine, no database. These pin the design
decisions the exercise asks students to defend: the reserved-name mapping
(``metadata_`` -> column ``"metadata"``), the 1536-dim nullable vector, the
CASCADE FK, the deliberate absence of a vector index, and — our addition — the
fact that the vector tables do not pollute the portable ``Base``.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector

from app.embedding_pipeline.store.models import (
    EMBEDDING_DIMENSIONS,
    ChunkRow,
    DocumentRow,
    RagBase,
)
from app.persistence.models import Base


def test_metadata_attribute_maps_to_metadata_column():
    assert DocumentRow.metadata_.expression.name == "metadata"
    assert ChunkRow.metadata_.expression.name == "metadata"


def test_embedding_is_a_nullable_1536_dim_vector():
    embedding = ChunkRow.__table__.c.embedding
    assert isinstance(embedding.type, Vector)
    assert embedding.type.dim == 1536
    assert embedding.type.dim == EMBEDDING_DIMENSIONS
    # Nullable on purpose: leaves the door open to insert-now-embed-later.
    assert embedding.nullable is True


def test_chunks_fk_cascades_on_document_delete():
    fk = next(iter(ChunkRow.__table__.c.document_id.foreign_keys))
    assert fk.column.table.name == "documents"
    assert fk.ondelete == "CASCADE"


def test_no_vector_index_yet():
    # The live session adds HNSW; the previo ships sequential scan only.
    index_columns = {col.name for index in ChunkRow.__table__.indexes for col in index.columns}
    assert "embedding" not in index_columns


def test_relational_indexes_present():
    index_names = {index.name for index in ChunkRow.__table__.indexes}
    assert index_names == {
        "ix_chunks_document_id",
        "ix_chunks_chunk_type",
        "ix_chunks_metadata_gin",
    }
    assert {index.name for index in DocumentRow.__table__.indexes} == {
        "ix_documents_source_path"
    }


def test_source_path_index_is_unique():
    """The 409 is the friendly path; the constraint is the guarantee."""
    index = next(iter(DocumentRow.__table__.indexes))
    assert index.unique is True
    assert {col.name for col in index.columns} == {"source_path"}


def test_vector_tables_live_on_their_own_base():
    """`Base.metadata.create_all` must keep working on SQLite.

    The ingestion tests build a temporary SQLite database from
    ``Base.metadata``; a ``Vector`` column there raises ``CompileError``. Keeping
    the RAG tables on ``RagBase`` is what makes the two compatible.
    """
    assert RagBase.metadata is not Base.metadata
    assert {"documents", "chunks"} == set(RagBase.metadata.tables)
    assert "documents" not in Base.metadata.tables
    assert "chunks" not in Base.metadata.tables

    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Vector), (
                f"{table.name}.{column.name} is a Vector on the portable base"
            )


def test_portable_base_still_builds_on_sqlite():
    """Compile every portable table for SQLite — the real regression guard."""
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable

    for table in Base.metadata.sorted_tables:
        DDL = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        assert "CREATE TABLE" in DDL
