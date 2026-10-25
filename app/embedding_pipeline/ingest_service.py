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
from collections.abc import Callable

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.embedder import OpenAIEmbedder
from app.embedding_pipeline.pseudonymize import (
    SupportsPseudonymize,
    pseudonymize_chunks,
)
from app.embedding_pipeline.schemas import Budget, IngestResponse
from app.embedding_pipeline.store.models import EMBEDDING_DIMENSIONS
from app.embedding_pipeline.store.repository import ChunkStore

log = structlog.get_logger(__name__)


class DuplicateDocumentError(Exception):
    """A document with the same ``source_path`` is already ingested."""

    def __init__(self, document_id: int) -> None:
        super().__init__(f"Document already ingested (id={document_id})")
        self.document_id = document_id


class PseudonymizationUnavailable(RuntimeError):
    """`pseudonymize: true` was requested but the service has no pseudonymizer.

    Refusing is the whole point: silently storing plaintext while reporting a
    successful ingest is the failure mode this flag exists to prevent.
    """


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
        pseudonymizer_factory: Callable[[], SupportsPseudonymize] | None = None,
    ) -> None:
        self._chunker = chunker
        self._embedder = embedder
        self._session_factory = session_factory
        self._store = store
        # A *factory*, not an instance: ConsistentPseudonymizer owns a Faker
        # whose ``seed_instance`` mutates shared state, so one instance across
        # concurrent requests would race. Building it per request is cheap
        # (the Presidio analyzer itself is cached).
        self._pseudonymizer_factory = pseudonymizer_factory

    async def ingest(
        self,
        *,
        source_path: str,
        document_type: str,
        budget: Budget,
        pseudonymize: bool = False,
    ) -> IngestResponse:
        started = time.perf_counter()

        try:
            document_id, embedded, entities = await self._persist(
                source_path=source_path,
                document_type=document_type,
                budget=budget,
                pseudonymize=pseudonymize,
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
            entities_pseudonymized=entities,
        )
        log.info("rag_ingest_persisted", source_path=source_path, **response.model_dump())
        return response

    async def _persist(
        self,
        *,
        source_path: str,
        document_type: str,
        budget: Budget,
        pseudonymize: bool = False,
    ) -> tuple[int, list, int]:
        """The transactional body: ``(document_id, embedded_chunks, entities)``."""
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

            # 3. Optional pseudonymization, applied to the chunk TEXT before it is
            #    embedded or persisted — so the vector matches the stored string
            #    and the plaintext never reaches Postgres. Failures propagate:
            #    the transaction rolls back rather than storing plaintext.
            entities = 0
            if pseudonymize:
                chunks, entities = await asyncio.to_thread(
                    self._pseudonymize, chunks
                )

            # 4. Batch embedding. The OpenAI client is sync; run it in a thread so
            #    the event loop keeps serving requests. If this raises, the
            #    transaction rolls back: no orphan document.
            embedded = await asyncio.to_thread(self._embedder.embed_many, chunks)

            dimensions = {len(chunk.embedding) for chunk in embedded}
            if dimensions != {EMBEDDING_DIMENSIONS}:
                raise EmbeddingDimensionMismatch(
                    f"provider returned vectors of dimension {sorted(dimensions)}; "
                    f"the store column is vector({EMBEDDING_DIMENSIONS})"
                )

            # 5. Document row + all chunk rows, still uncommitted.
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
            # 6. Commit happens on scope exit.
            return document_id, embedded, entities

    def _pseudonymize(self, chunks: list) -> tuple[list, int]:
        """Build a pseudonymizer and rewrite the chunks (runs in a worker thread)."""
        if self._pseudonymizer_factory is None:
            # Requested but not wired: refuse rather than persist plaintext.
            raise PseudonymizationUnavailable(
                "pseudonymization was requested but no pseudonymizer is configured"
            )
        return pseudonymize_chunks(chunks, self._pseudonymizer_factory())
