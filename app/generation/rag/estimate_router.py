"""HTTP surface for the T0B grounded-estimation chain.

Thin transport, in the style of ``retrieval/router.py``: the body models validate
bounds (422 before any I/O), the orchestrator owns the chain, and every failure is
mapped from the domain error to a generic body while the reason and exception type
go to the log. No auth, consistent with the rest of this repository.

Error mapping for ``POST /from-transcript`` (each error class carries the status
its router should use — see ``errors.py``):

* ``GenerationError`` / ``ReformulationError`` / ``CitationValidationError`` -> **502**
* ``ContextAssemblyError`` -> **422**
* anything else -> **500** generic (never the exception text: this route is
  unauthenticated and provider messages can carry payload details)

The ``/stages/*`` routes exist so each step can be inspected by hand while
debugging a chain run. They are stateless and reuse the same functions the chain
calls — never a second implementation — and they are not a second public API.
Each answers with :class:`StageEnvelope` fields plus its own payload, because the
point of the route is to show which step produced what (and how long it took).
"""

from __future__ import annotations

import time
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings, get_settings
from app.embedding_pipeline.schemas import MAX_QUERY_CHARS, MAX_SEARCH_K
from app.generation.rag.errors import (
    ChainError,
    ContextAssemblyError,
    ReformulationError,
)
from app.generation.rag.estimator import generate_estimate
from app.generation.rag.orchestrator import estimate_from_transcript
from app.generation.rag.retrieval.errors import RerankerUnavailable, RetrievalUnavailable
from app.generation.rag.retrieval.pipeline import retrieve
from app.generation.rag.retrieval.schemas import (
    RetrievedChunk,
    RetrievalResult,
    SearchMode,
)
from app.generation.rag.schemas import (
    MAX_TRANSCRIPT_CHARS,
    MIN_TRANSCRIPT_CHARS,
    EstimateFromTranscriptRequest,
    EstimateFromTranscriptResponse,
    EstimationQuery,
    GroundedEstimate,
    StageEnvelope,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/estimate", tags=["estimate-rag"])

PUBLIC_GENERIC_ERROR = "Failed to estimate from the transcript."
PUBLIC_EMBEDDER_ERROR = "Embedding service is not available."
PUBLIC_RERANKER_ERROR = "Reranking service is unavailable. Please retry later."
PUBLIC_RETRIEVAL_ERROR = "Failed to run retrieval."


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since ``started``, never negative."""
    return int(max(0.0, (time.perf_counter() - started) * 1000))


# --- request models (local on purpose: these routes are not a public API) ----


class StageReformulateRequest(BaseModel):
    """Body of ``POST /v1/estimate/stages/reformulate``."""

    model_config = ConfigDict(extra="forbid")

    transcript: str = Field(min_length=MIN_TRANSCRIPT_CHARS, max_length=MAX_TRANSCRIPT_CHARS)


class StageRetrieveRequest(BaseModel):
    """Body of ``POST /v1/estimate/stages/retrieve``.

    Exactly one of ``query`` (free text) or ``estimation_query`` (a previously
    reformulated object) must be supplied, so the caller can inspect retrieval
    with or without the reformulation step.
    """

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY_CHARS)
    estimation_query: EstimationQuery | None = None
    top_k: int = Field(default=5, ge=1, le=MAX_SEARCH_K)
    search_mode: SearchMode | None = Field(
        default=None, description="None defers to RETRIEVAL_SEARCH_MODE."
    )
    rerank: bool | None = Field(default=None, description="None defers to RERANKER_ENABLED.")
    rrf_k: int | None = Field(default=None, ge=1, le=1_000)
    distance_threshold: float | None = Field(default=None, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def _require_exactly_one_query(self) -> "StageRetrieveRequest":
        if (self.query is None) == (self.estimation_query is None):
            raise ValueError("Provide exactly one of 'query' or 'estimation_query'.")
        return self

    @property
    def search_text(self) -> str:
        """The string that is embedded: the canonical query wins when both could exist."""
        if self.estimation_query is not None:
            return self.estimation_query.search_query
        return self.query or ""


class StageAssembleRequest(BaseModel):
    """Body of ``POST /v1/estimate/stages/assemble``."""

    model_config = ConfigDict(extra="forbid")

    chunks: list[RetrievedChunk] = Field(min_length=1)
    max_tokens: int = Field(
        default=3000, ge=1, description="Context budget forwarded to the assembler."
    )


class StageGenerateRequest(BaseModel):
    """Body of ``POST /v1/estimate/stages/generate``.

    ``source_ids`` is what the caller claims the context contains. This endpoint
    does not validate citations — validation is a chain policy, and the stage
    routes are for inspecting one step at a time.
    """

    model_config = ConfigDict(extra="forbid")

    query: EstimationQuery
    context: str = Field(default="", max_length=200_000)
    source_ids: list[int] = Field(default_factory=list)
    model_override: str | None = Field(
        default=None, description="Per-call model override; None uses the deployment default."
    )


# --- response models --------------------------------------------------------


class StageReformulateResponse(StageEnvelope):
    """``StageEnvelope`` plus the reformulator's object."""

    stage: Literal["reformulate"] = "reformulate"
    latency_ms: int = Field(default=0, ge=0)
    query: EstimationQuery


class StageRetrieveResponse(StageEnvelope):
    """The retrieval result plus the stage envelope.

    The result is nested whole (``result``) rather than flattened by hand: a
    field-by-field copy would silently drop any field ``RetrievalResult`` gains
    later. ``chunks`` is mirrored at the top level so every stage body answers
    "what did this step produce" at the same key.
    """

    stage: Literal["retrieve"] = "retrieve"
    latency_ms: int = Field(default=0, ge=0)
    result: RetrievalResult
    chunks: list[RetrievedChunk] = Field(default_factory=list)


class StageAssembleResponse(StageEnvelope):
    """Assembled context plus the ids an estimate may cite from it."""

    stage: Literal["assemble"] = "assemble"
    latency_ms: int = Field(default=0, ge=0)
    context: str
    sources: list[int] = Field(description="Chunk ids present in the context, sorted.")


class StageGenerateResponse(StageEnvelope):
    """``StageEnvelope`` plus the generated estimate."""

    stage: Literal["generate"] = "generate"
    latency_ms: int = Field(default=0, ge=0)
    estimate: GroundedEstimate


# --- dependencies and error mapping ----------------------------------------


def get_estimate_settings() -> Settings:
    """Settings for one estimate request.

    A named dependency rather than a direct ``get_settings()`` call so tests can
    override it with ``app.dependency_overrides``.
    """
    return get_settings()


def _chain_error(exc: ChainError, *, stage: str) -> HTTPException:
    """Map a domain chain error to the status and public body it declares.

    The status lives on the error class (``errors.py``) so the mapping cannot
    drift per route; only the logging context differs.
    """
    log.error(
        "estimate_chain_failed",
        stage=stage,
        reason=type(exc).__name__,
        status_code=exc.status_code,
        error=str(exc)[:300],
    )
    return HTTPException(status_code=exc.status_code, detail=exc.public_message)


# --- routes -----------------------------------------------------------------


@router.post(
    "/from-transcript",
    response_model=EstimateFromTranscriptResponse,
    responses={
        422: {"description": "Body validation failed, or no context to assemble"},
        500: {"description": "Unexpected chain failure"},
        502: {"description": "Reformulation, generation or citation validation failed"},
    },
)
async def estimate_route(
    request: EstimateFromTranscriptRequest,
) -> EstimateFromTranscriptResponse:
    """Run the whole chain: transcript in, grounded estimate with evidence out."""
    try:
        return await estimate_from_transcript(request)
    except ChainError as exc:
        raise _chain_error(exc, stage="from_transcript") from exc
    except Exception as exc:  # noqa: BLE001 - never leak provider text to the client.
        log.error(
            "estimate_chain_failed",
            stage="from_transcript",
            reason="unexpected_error",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=PUBLIC_GENERIC_ERROR) from exc


@router.post(
    "/stages/reformulate",
    response_model=StageReformulateResponse,
    responses={502: {"description": "The transcript could not be interpreted"}},
)
async def stage_reformulate(request: StageReformulateRequest) -> StageReformulateResponse:
    """Transcript -> ``EstimationQuery``, without retrieving anything."""
    # Lazy import: a peer owns this module and its signature is the contract.
    from app.generation.rag.query_reformulator import reformulate

    started = time.perf_counter()
    try:
        query = await reformulate(request.transcript)
    except ReformulationError as exc:
        raise _chain_error(exc, stage="reformulate") from exc
    except Exception as exc:  # noqa: BLE001
        log.error(
            "estimate_stage_failed",
            stage="reformulate",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=PUBLIC_GENERIC_ERROR) from exc

    return StageReformulateResponse(latency_ms=_elapsed_ms(started), query=query)


@router.post(
    "/stages/retrieve",
    response_model=StageRetrieveResponse,
    responses={
        500: {"description": "No embedder or vector store is available"},
        502: {"description": "The requested rerank step failed"},
    },
)
async def stage_retrieve(
    request: StageRetrieveRequest,
    settings: Settings = Depends(get_estimate_settings),
) -> StageRetrieveResponse:
    """Run the Session-10 pipeline for a plain query or a reformulated one."""
    search_mode = request.search_mode or settings.RETRIEVAL_SEARCH_MODE
    rerank = settings.RERANKER_ENABLED if request.rerank is None else request.rerank
    rrf_k = settings.RRF_K if request.rrf_k is None else request.rrf_k

    started = time.perf_counter()
    try:
        result = await retrieve(
            query=request.search_text,
            top_k=request.top_k,
            recall_k=settings.RETRIEVAL_RECALL_TOP_K,
            search_mode=search_mode,
            rerank=rerank,
            rrf_k=rrf_k,
            rerank_top_n=settings.RERANK_TOP_N,
            distance_threshold=request.distance_threshold,
        )
    except RerankerUnavailable as exc:
        log.error("estimate_stage_failed", stage="retrieve", reason="reranker_unavailable")
        raise HTTPException(status_code=502, detail=PUBLIC_RERANKER_ERROR) from exc
    except RetrievalUnavailable as exc:
        log.error("estimate_stage_failed", stage="retrieve", reason="retrieval_unavailable")
        raise HTTPException(status_code=500, detail=PUBLIC_EMBEDDER_ERROR) from exc
    except Exception as exc:  # noqa: BLE001
        log.error(
            "estimate_stage_failed",
            stage="retrieve",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=PUBLIC_RETRIEVAL_ERROR) from exc

    return StageRetrieveResponse(
        latency_ms=_elapsed_ms(started), result=result, chunks=result.chunks
    )


@router.post(
    "/stages/assemble",
    response_model=StageAssembleResponse,
    responses={422: {"description": "No context could be built from the chunks"}},
)
async def stage_assemble(request: StageAssembleRequest) -> StageAssembleResponse:
    """Chunks -> ``<source>`` context, plus the ids an estimate may cite."""
    # Lazy import: a peer owns this module and its signature is the contract.
    from app.generation.rag.context_assembler import assemble, sources_of

    started = time.perf_counter()
    try:
        context = assemble(request.chunks, max_tokens=request.max_tokens)
        sources = sources_of(request.chunks, max_tokens=request.max_tokens)
    except ContextAssemblyError as exc:
        raise _chain_error(exc, stage="assemble") from exc
    except Exception as exc:  # noqa: BLE001
        log.error(
            "estimate_stage_failed",
            stage="assemble",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=PUBLIC_GENERIC_ERROR) from exc

    return StageAssembleResponse(
        latency_ms=_elapsed_ms(started), context=context, sources=sorted(sources)
    )


@router.post(
    "/stages/generate",
    response_model=StageGenerateResponse,
    responses={502: {"description": "The estimation model call failed"}},
)
async def stage_generate(request: StageGenerateRequest) -> StageGenerateResponse:
    """Run one grounded generation turn over a caller-supplied context."""
    started = time.perf_counter()
    try:
        estimate = await generate_estimate(
            request.query,
            request.context,
            source_ids=set(request.source_ids),
            model=request.model_override,
        )
    except ChainError as exc:
        raise _chain_error(exc, stage="generate") from exc
    except Exception as exc:  # noqa: BLE001
        log.error(
            "estimate_stage_failed",
            stage="generate",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=500, detail=PUBLIC_GENERIC_ERROR) from exc

    return StageGenerateResponse(latency_ms=_elapsed_ms(started), estimate=estimate)
