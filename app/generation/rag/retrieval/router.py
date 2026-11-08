"""HTTP surface for the Session-10 retrieval pipeline.

Thin transport, mirroring ``app/embedding_pipeline/search_router.py``: the body
model validates bounds (422 before any I/O), the pipeline owns every ranking
decision, and failures are mapped to generic bodies while the reason and the
exception type go to the log.

Error mapping:

* ``RerankerUnavailable`` -> **502** — the caller asked for ``rerank=true`` and
  the cross-encoder could not score. Fail-closed: no silent recall order.
* ``RetrievalUnavailable`` -> **500** — no embedder or no vector store wired.
* anything else -> **500** generic.

No auth, consistent with the rest of this repository.

Every collaborator arrives through ``app.dependencies`` so a test can replace it
with ``app.dependency_overrides``. ``recall_k`` has a schema default, so the
route can tell "the client omitted it" (use ``RETRIEVAL_RECALL_TOP_K``) from
"the client asked for 50".
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.dependencies import get_chunk_store, get_embedder, get_reranker
from app.generation.rag.retrieval.errors import RerankerUnavailable, RetrievalUnavailable
from app.generation.rag.retrieval.pipeline import retrieve
from app.generation.rag.retrieval.schemas import RetrievalRequest, RetrievalResult

log = structlog.get_logger(__name__)

router = APIRouter(tags=["retrieval"])

PUBLIC_RERANKER_ERROR = "Reranking service is unavailable. Please retry later."
PUBLIC_EMBEDDER_ERROR = "Embedding service is not available."
PUBLIC_GENERIC_ERROR = "Failed to run retrieval."


def get_retrieval_settings() -> Settings:
    """Settings for one retrieval request.

    A named dependency rather than a direct ``get_settings()`` call so tests can
    patch this module's ``get_settings`` attribute or override the dependency —
    both conventions exist in this suite.
    """
    return get_settings()


def get_retrieval_session_factory():
    """Async session factory for the retrieval branches.

    Wraps ``get_async_session_factory`` so FastAPI does not expose its optional
    ``url`` argument as a query parameter — a client must not be able to point
    retrieval at a different database.
    """
    from app.dependencies import get_async_session_factory

    return get_async_session_factory()


def get_retrieval_reranker(request: Request) -> Any:
    """The cross-encoder, or ``None`` when it cannot even be constructed.

    Construction is cheap and imports no model runtime, but it does read
    ``Settings`` (for ``RERANKER_MODEL``). Returning ``None`` instead of raising
    keeps the un-reranked configurations usable in a deployment whose settings
    are incomplete: ``rerank=false`` never needs a reranker. When ``rerank=true``
    the pipeline resolves the factory itself and fails closed with
    ``RerankerUnavailable`` -> 502, so nothing is silently skipped.

    ``app.dependency_overrides[get_reranker]`` is consulted explicitly because
    this function calls the factory directly rather than declaring it as a
    sub-dependency, which is what allows the failure above to be tolerated.
    """
    factory = request.app.dependency_overrides.get(get_reranker, get_reranker)
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 - only reachable when asked to rerank.
        log.warning(
            "reranker_unavailable",
            reason="construction_failed",
            error_type=type(exc).__name__,
        )
        return None


@router.post("/v1/retrieval/search", response_model=RetrievalResult)
async def retrieval_search(
    request: RetrievalRequest,
    settings: Settings = Depends(get_retrieval_settings),
    embedder=Depends(get_embedder),
    session_factory=Depends(get_retrieval_session_factory),
    store=Depends(get_chunk_store),
    reranker: Any = Depends(get_retrieval_reranker),
) -> RetrievalResult:
    """Run one of the four vector/hybrid x rerank configurations."""
    # Request field wins; ``None`` means "use the deployment default". ``recall_k``
    # is checked against ``model_fields_set`` because it has a schema default of
    # its own (50) rather than ``None``.
    search_mode = request.search_mode or settings.RETRIEVAL_SEARCH_MODE
    rerank = settings.RERANKER_ENABLED if request.rerank is None else request.rerank
    rrf_k = settings.RRF_K if request.rrf_k is None else request.rrf_k
    recall_k = (
        request.recall_k
        if "recall_k" in request.model_fields_set
        else settings.RETRIEVAL_RECALL_TOP_K
    )

    if embedder is None or store is None:
        log.error(
            "retrieval_failed",
            reason="retrieval_unavailable",
            detail="embedder_or_store_unavailable",
        )
        raise HTTPException(status_code=500, detail=PUBLIC_EMBEDDER_ERROR)

    try:
        result = await retrieve(
            query=request.query,
            top_k=request.top_k,
            recall_k=recall_k,
            search_mode=search_mode,
            rerank=rerank,
            rrf_k=rrf_k,
            rerank_top_n=settings.RERANK_TOP_N,
            distance_threshold=request.distance_threshold,
            embedder=embedder,
            session_factory=session_factory,
            store=store,
            reranker=reranker,
        )
    except RerankerUnavailable as exc:
        log.error(
            "retrieval_failed",
            reason="reranker_unavailable",
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail=PUBLIC_RERANKER_ERROR) from exc
    except RetrievalUnavailable as exc:
        log.error(
            "retrieval_failed",
            reason="retrieval_unavailable",
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail=PUBLIC_EMBEDDER_ERROR) from exc
    except Exception as exc:  # noqa: BLE001 - embedding/DB failures become a 500.
        log.error(
            "retrieval_failed",
            reason="retrieval_error",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=PUBLIC_GENERIC_ERROR) from exc

    log.info(
        "retrieval_query",
        query=request.query[:80],
        mode=result.search_mode,
        rerank=result.rerank,
        results=len(result.chunks),
        search_time_ms=result.search_time_ms,
    )
    return result
