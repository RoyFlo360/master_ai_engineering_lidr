"""Async data-access layer for the vector store.

The store never opens or commits sessions: the caller (ingest service, retriever)
owns the ``AsyncSession`` so a whole ingest — duplicate check, document row,
chunk rows — fits in ONE transaction. A failure anywhere rolls everything back
and leaves no orphan ``documents`` row.
"""

from __future__ import annotations

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embedding_pipeline.schemas import EmbeddedChunk
from app.embedding_pipeline.store.models import ChunkRow, DocumentRow

# The structural chunker emits one chunk per budget component; the vocabulary is
# queryable thanks to the index on ``chunk_type`` (live-session filters).
BUDGET_COMPONENT = "budget_component"


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
        self, session: AsyncSession, *, query_vector: list[float], k: int
    ) -> list[Row]:
        """k nearest chunks by cosine distance (``<=>``), sequential scan.

        Cosine over L2/inner product: OpenAI embeddings are normalized so the
        ranking would be equivalent, but cosine keeps us aligned with the RAG
        literature AND with the ``vector_cosine_ops`` operator class of the HNSW
        index the live session adds — an operator/index mismatch makes Postgres
        silently ignore the index.

        No vector index exists yet, so this is a sequential scan on purpose: it
        is the baseline the live session measures the index against.
        """
        distance = ChunkRow.embedding.cosine_distance(query_vector)
        stmt = (
            select(
                ChunkRow.id.label("chunk_id"),
                ChunkRow.document_id.label("document_id"),
                ChunkRow.chunk_type.label("chunk_type"),
                ChunkRow.content.label("content"),
                ChunkRow.metadata_.label("metadata"),
                distance.label("distance"),
            )
            .order_by(distance)
            .limit(k)
        )
        return list((await session.execute(stmt)).all())
