"""Async data-access layer for the vector store.

The store never opens or commits sessions: the caller (ingest service, retriever)
owns the ``AsyncSession`` so a whole ingest — duplicate check, document row,
chunk rows — fits in ONE transaction. A failure anywhere rolls everything back
and leaves no orphan ``documents`` row.
"""

from __future__ import annotations

from sqlalchemy import Row, cast, column, func, select, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_pipeline.schemas import EmbeddedChunk
from app.embedding_pipeline.store.models import (
    HNSW_EF_SEARCH,
    ChunkRow,
    DocumentRow,
)

try:  # The cast target for the query-side text-search configuration.
    from sqlalchemy.dialects.postgresql import REGCONFIG as _REGCONFIG_TYPE
except ImportError:  # pragma: no cover - every supported SQLAlchemy provides it
    _REGCONFIG_TYPE = None

# The structural chunker emits one chunk per budget component; the vocabulary is
# queryable thanks to the index on ``chunk_type`` (live-session filters).
BUDGET_COMPONENT = "budget_component"

#: Text-search configuration for the lexical branch (Session 10).
#:
#: The value is baked into the stored generated column by migration
#: ``0004_rag_fts`` as a literal, and the query side must use the identical
#: configuration or Postgres silently bypasses the GIN index — which is a silent
#: slowdown, not an error. So the value lives here, next to the SQL that uses it,
#: and a test parses both this constant, the migration literal and ``Settings``
#: and fails when they drift. Changing it is a migration (DROP/ADD COLUMN plus a
#: GIN rebuild), never an ``.env`` edit.
FTS_REGCONFIG = "english"


class ChunkStore:
    """CRUD + similarity search over ``documents`` / ``chunks``.

    Stateless on purpose: no session, no engine, no cache. Everything that
    matters is passed in, which is also what makes it trivially fakeable in the
    default (hermetic) test suite.
    """

    async def find_document_id(self, session: AsyncSession, source_path: str) -> int | None:
        """Return the id of the document already ingested from ``source_path``."""
        stmt = select(DocumentRow.id).where(DocumentRow.source_path == source_path)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def persist_document_with_chunks(
        self,
        session: AsyncSession,
        *,
        source_path: str,
        document_type: str,
        doc_metadata: dict,
        embedded_chunks: list[EmbeddedChunk],
    ) -> int:
        """Insert the document row plus all its chunk rows.

        No commit here — the caller's transaction decides when (and whether)
        anything lands. ``flush()`` assigns ``document.id`` without committing,
        which is what lets the chunk rows carry the foreign key inside the same
        transaction.
        """
        document = DocumentRow(
            source_path=source_path,
            document_type=document_type,
            metadata_=doc_metadata,
        )
        session.add(document)
        await session.flush()  # assigns document.id without committing

        session.add_all(
            ChunkRow(
                document_id=document.id,
                chunk_type=BUDGET_COMPONENT,
                content=chunk.text,
                embedding=chunk.embedding,
                metadata_=chunk.metadata,
            )
            for chunk in embedded_chunks
        )
        return document.id

    async def search(
        self,
        session: AsyncSession,
        *,
        query_vector: list[float],
        k: int,
        distance_threshold: float | None = None,
    ) -> list[Row]:
        """k nearest chunks by cosine distance (``<=>``) through the HNSW index.

        Cosine over L2/inner product: OpenAI embeddings are normalized so the
        ranking would be equivalent, but cosine keeps us aligned with the RAG
        literature AND with the ``vector_cosine_ops`` operator class of
        ``ix_chunks_embedding_hnsw`` — an operator/index mismatch makes Postgres
        silently ignore the index and fall back to a sequential scan.

        ``SET LOCAL hnsw.ef_search`` is applied inside the session's implicit
        transaction: ``SET LOCAL`` scopes the setting to that transaction, so a
        tuned value never leaks to other statements on the same pooled
        connection. It has no effect on SQLite, which is why the statement is
        guarded by dialect.

        ``distance_threshold`` is an optional relevance floor (Session 10). It
        filters the vector branch only; the lexical branch has its own scoring
        and is fused untresholded. When it is ``None`` the statement is exactly
        the pre-Session-10 query, which existing callers rely on.
        """
        if session.get_bind().dialect.name == "postgresql":
            # The value is a module constant (int), never user input; SET does
            # not accept bind parameters.
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"))

        distance = ChunkRow.embedding.cosine_distance(query_vector)
        stmt = select(
            ChunkRow.id.label("chunk_id"),
            ChunkRow.document_id.label("document_id"),
            ChunkRow.chunk_type.label("chunk_type"),
            ChunkRow.content.label("content"),
            ChunkRow.metadata_.label("metadata"),
            distance.label("distance"),
        )
        if distance_threshold is not None:
            # Filter on the expression, not the SELECT alias: Postgres does not
            # allow an output alias in WHERE.
            stmt = stmt.where(distance <= distance_threshold)
        stmt = stmt.order_by(distance).limit(k)
        return list((await session.execute(stmt)).all())

    async def search_lexical(
        self, session: AsyncSession, *, query_text: str, top_k: int = 50
    ) -> list[Row]:
        """Full-text search over ``chunks.content`` through the GIN index.

        ``content_tsv`` is a Postgres-only ``GENERATED ALWAYS AS
        (to_tsvector(<regconfig>, content)) STORED`` column added by migration
        ``0004_rag_fts``. It is deliberately **not** mapped on :class:`ChunkRow`:
        the ORM model would then fail to compile on SQLite (the hermetic test
        suite builds the schema with ``create_all``). The column is referenced
        by name instead.

        ``plainto_tsquery`` (not ``to_tsquery``) treats the query as plain text:
        no operator syntax to escape, no ``syntax error in tsquery`` for a raw
        transcript containing ``:``, ``&`` or ``|``.

        The row labels mirror :meth:`search` so the retrieval pipeline maps both
        branches the same way. ``rank`` is ``ts_rank_cd``; there is no distance.
        """
        # Exactly one configuration for the query side: the module constant the
        # migration's literal is pinned against by
        # ``tests/test_fts_regconfig.py``. Reading it from anywhere else (an
        # ``.env`` setting, a request field) is how the GIN index gets bypassed
        # silently.
        regconfig = FTS_REGCONFIG
        content_tsv = column("content_tsv", TSVECTOR)
        # Cast the setting explicitly: without it the server sees an untyped
        # parameter and the overload resolution is not guaranteed to pick
        # ``plainto_tsquery(regconfig, text)``.
        regconfig_expr = cast(regconfig, _REGCONFIG_TYPE) if _REGCONFIG_TYPE else regconfig
        tsquery = func.plainto_tsquery(regconfig_expr, query_text)
        rank = func.ts_rank_cd(content_tsv, tsquery).label("rank")
        stmt = (
            select(
                ChunkRow.id.label("chunk_id"),
                ChunkRow.document_id.label("document_id"),
                ChunkRow.chunk_type.label("chunk_type"),
                ChunkRow.content.label("content"),
                ChunkRow.metadata_.label("metadata"),
                rank,
            )
            .where(content_tsv.op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(top_k)
        )
        return list((await session.execute(stmt)).all())
