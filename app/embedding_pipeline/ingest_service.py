"""Ingest orchestration: chunk -> embed -> persist, in ONE transaction.

Composes the structural chunker, the OpenAI embedder and the pgvector store.
Each of them is independently testable; this module is the only place they meet.

The single transaction is the point: the document row is inserted first (so
chunks can FK it via ``flush``), but if the embeddings API fails afterwards the
whole transaction rolls back and no orphan ``documents`` row survives.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.embedder import OpenAIEmbedder
from app.embedding_pipeline.schemas import Budget, IngestResponse
from app.embedding_pipeline.store.models import EMBEDDING_DIMENSIONS
from app.embedding_pipeline.store.repository import ChunkStore

log = structlog.get_logger(__name__)


class DuplicateDocumentError(Exception):
    """A document with the same ``source_path`` is already ingested."""

    def __init__(self, document_id: int) -> None:
        super().__init__(f"Document already ingested (id={document_id})")
        self.document_id = document_id


class EmbeddingDimensionMismatch(RuntimeError):
    """The provider returned vectors that do not match the stored column width.

    Storing them would make every later distance meaningless, so this refuses
    instead of writing a poisoned row.
    """


class RagIngestService:
    """Persists one budget as a document + its embedded chunks."""

    def __init__(
        self,
        chunker: JSONStructuralChunker,
        embedder: OpenAIEmbedder,
        session_factory: async_sessionmaker,
        store: ChunkStore,
    ) -> None:
        self._chunker = chunker
        self._embedder = embedder
        self._session_factory = session_factory
        self._store = store

    async def ingest(
        self, *, source_path: str, document_type: str, budget: Budget
    ) -> IngestResponse:
        started = time.perf_counter()

        try:
            document_id, embedded = await self._persist(
                source_path=source_path,
                document_type=document_type,
                budget=budget,
            )
        except IntegrityError as exc:
            # Lost the race against a concurrent identical ingest: the unique
            # index fired. The transaction is already rolled back, so the lookup
            # runs in a fresh session (a SELECT inside an aborted Postgres
            # transaction fails with "current transaction is aborted").
            async with self._session_factory() as session:
                existing_id = await self._store.find_document_id(session, source_path)
            if existing_id is None:
                raise
            log.info(
                "rag_ingest_duplicate_race",
                source_path=source_path,
                document_id=existing_id,
            )
            raise DuplicateDocumentError(existing_id) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response = IngestResponse(
            document_id=document_id,
            chunks_created=len(embedded),
            embedding_dimension=len(embedded[0].embedding),
            ingestion_time_ms=elapsed_ms,
        )
        log.info("rag_ingest_persisted", source_path=source_path, **response.model_dump())
        return response

    async def _persist(
        self, *, source_path: str, document_type: str, budget: Budget
    ) -> tuple[int, list]:
        """The transactional body: returns ``(document_id, embedded_chunks)``."""
        async with self._session_factory() as session, session.begin():
            # 1. Duplicate guard. Friendly 409; the unique index is the guarantee
            #    for the concurrent case handled in ``ingest``.
            existing_id = await self._store.find_document_id(session, source_path)
            if existing_id is not None:
                raise DuplicateDocumentError(existing_id)

            # 2. Structural chunking (one chunk per budget component).
            chunks = self._chunker.chunk([budget])
            if not chunks:  # pragma: no cover - Budget requires >= 1 component
                raise ValueError("budget produced no chunks")

            # 3. Batch embedding. The OpenAI client is sync; run it in a thread so
            #    the event loop keeps serving requests. If this raises, the
            #    transaction rolls back: no orphan document.
            embedded = await asyncio.to_thread(self._embedder.embed_many, chunks)

            dimensions = {len(chunk.embedding) for chunk in embedded}
            if dimensions != {EMBEDDING_DIMENSIONS}:
                raise EmbeddingDimensionMismatch(
                    f"provider returned vectors of dimension {sorted(dimensions)}; "
                    f"the store column is vector({EMBEDDING_DIMENSIONS})"
                )

            # 4. Document row + all chunk rows, still uncommitted.
            document_id = await self._store.persist_document_with_chunks(
                session,
                source_path=source_path,
                document_type=document_type,
                doc_metadata={
                    "budget_id": budget.budget_id,
                    "client_sector": budget.client_metadata.sector,
                    "year": budget.year,
                },
                embedded_chunks=embedded,
            )
            # 5. Commit happens on scope exit.
            return document_id, embedded
