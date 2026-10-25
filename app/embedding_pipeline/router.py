"""HTTP layer for the embedding pipeline (Session 8).

Thin router: it maps service exceptions to status codes. The chunk -> embed ->
persist orchestration lives in ``RagIngestService``; no business logic here.

Client-facing error bodies are deliberately generic; the reason and the
exception type go to the log. An exception message can carry request content or
provider details, and this endpoint is unauthenticated.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.dependencies import (
    ALL_STRATEGIES,
    build_chunkers,
    get_embedder,
    get_rag_ingest_service,
    vector_store_problem,
)
from app.embedding_pipeline.analysis.comparison import (
    ChunkingComparator,
    CompareRequest,
    CompareResponse,
)
from app.embedding_pipeline.embedder import OpenAIEmbedder
from app.embedding_pipeline.ingest_service import (
    DuplicateDocumentError,
    PseudonymizationUnavailable,
    RagIngestService,
)
from app.embedding_pipeline.schemas import IngestRequest, IngestResponse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


@router.post(
    "/ingest",
    response_model=IngestResponse,
    responses={409: {"description": "Document already ingested"}},
)
async def ingest(
    request: IngestRequest,
    service: RagIngestService | None = Depends(get_rag_ingest_service),
) -> IngestResponse | JSONResponse:
    """Persist one budget as a document + embedded chunks (one transaction)."""
    if service is None:
        # No OPENAI_API_KEY configured, DATABASE_URL is not Postgres, or
        # EMBEDDING_MODEL does not match the stored vector width. Generic
        # message to the client; the reason is logged.
        log.error(
            "embeddings_ingest_failed",
            reason="service_unavailable",
            detail=vector_store_problem() or "embedder_unavailable",
        )
        raise HTTPException(status_code=500, detail="Embedding service is not available.")

    log.info(
        "embeddings_ingest_received",
        source_path=request.source_path,
        document_type=request.document_type,
        pseudonymize=request.pseudonymize,
    )
    try:
        return await service.ingest(
            source_path=request.source_path,
            document_type=request.document_type,
            budget=request.content,
            pseudonymize=request.pseudonymize,
        )
    except PseudonymizationUnavailable as exc:
        # A configuration gap, not a client error: the caller asked for PII
        # protection the deployment cannot provide. Never fall back to storing
        # plaintext.
        log.error(
            "embeddings_ingest_failed",
            reason="pseudonymizer_unavailable",
            source_path=request.source_path,
        )
        raise HTTPException(
            status_code=503, detail="Pseudonymization is not available."
        ) from exc
    except DuplicateDocumentError as exc:
        # JSONResponse (not HTTPException) to keep the exercise's literal
        # top-level shape: {"detail": ..., "document_id": ...}.
        log.info(
            "embeddings_ingest_duplicate",
            source_path=request.source_path,
            document_id=exc.document_id,
        )
        return JSONResponse(
            status_code=409,
            content={"detail": "Document already ingested", "document_id": exc.document_id},
        )
    except Exception as exc:  # noqa: BLE001 - embedding/DB failures become a 500.
        log.error(
            "embeddings_ingest_failed",
            reason="ingest_error",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail="Failed to generate embeddings.") from exc


@router.post("/compare", response_model=CompareResponse)
def compare(
    request: CompareRequest,
    embedder: OpenAIEmbedder | None = Depends(get_embedder),
) -> CompareResponse:
    """Run several chunking strategies over the same budgets and compare them.

    Returns per-strategy corpus stats and, if queries are given, the top-k chunks
    each strategy retrieves. **Nothing is persisted** — this is the analysis
    surface, not the ingest path.
    """
    if embedder is None:
        log.error("embeddings_compare_failed", reason="embedder_unavailable")
        raise HTTPException(status_code=500, detail="Embedding service is not available.")

    names = request.strategies or ALL_STRATEGIES
    try:
        chunkers = build_chunkers(names)
    except KeyError as exc:
        # Unknown strategy is a client error: 400, never a silent 200.
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {exc.args[0]}") from exc
    except RuntimeError as exc:
        # A strategy needs a provider key that is not configured.
        log.error("embeddings_compare_failed", reason="missing_api_key", error=str(exc)[:200])
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    comparator = ChunkingComparator(chunkers, embedder)
    log.info(
        "embeddings_compare_received",
        total_budgets=len(request.budgets),
        strategies=names,
        n_queries=len(request.queries),
    )
    try:
        stats = comparator.compute_stats(request.budgets)
        queries = comparator.run_queries(request.budgets, request.queries, request.top_k)
    except Exception as exc:  # noqa: BLE001 - any chunker/embedding failure becomes a 500
        log.error(
            "embeddings_compare_failed",
            reason="comparison_error",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(
            status_code=500, detail="Failed to run chunking comparison."
        ) from exc

    return CompareResponse(stats_per_strategy=stats, queries_per_strategy=queries)

