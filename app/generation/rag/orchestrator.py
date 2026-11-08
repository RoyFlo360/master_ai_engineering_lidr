"""The T0B chain: transcript -> query -> chunks -> context -> estimate -> response.

The order is load-bearing and deliberately explicit here rather than hidden behind
a framework:

1. **idempotency lookup** — a replay must not pay for reformulation, retrieval or
   generation. Only runs when the client sent a key.
2. **reformulate** — transcript to ``EstimationQuery`` (peer module, imported
   lazily so this file imports even while that module is being written).
3. **retrieve** — ``search_query`` through the Session-10 pipeline. ``None`` on
   mode/rerank means "use ``Settings``".
4. **assemble** — chunks to ``<source>`` blocks; the same chunk list yields the
   ids the estimate is allowed to cite.
5. **generate** — one LLM turn (``estimator``).
6. **validate** — ``check_citations``; on failure exactly one corrective retry
   naming the invalid ids, then ``CitationValidationError``.
7. **store + return** — the completed response goes back under the idempotency
   key so step 1 can short-circuit next time.

Empty retrieval short-circuits before steps 5-6: there is nothing to ground the
estimate in, so the chain returns ``grounded=False`` with zero retries instead of
asking the model to answer from memory.

Peer modules (``query_reformulator``, ``context_assembler``) are imported inside
the functions on purpose: their signatures are the contract, not their import
order, and a lazy import keeps this module importable mid-change.
"""

from __future__ import annotations

import time

import structlog

from app.config import get_settings
from app.generation.rag.errors import CitationValidationError
from app.generation.rag.estimator import generate_estimate, unavailable_estimate
from app.generation.rag.idempotency import get_idempotency_store, make_key
from app.generation.rag.observability import log_stage
from app.generation.rag.retrieval.schemas import RetrievedChunk
from app.generation.rag.schemas import (
    EstimateFromTranscriptRequest,
    EstimateFromTranscriptResponse,
    EstimationQuery,
    GroundedEstimate,
)
from app.generation.rag.validation import check_citations

log = structlog.get_logger(__name__)


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since ``started``, never negative."""
    return int(max(0.0, (time.perf_counter() - started) * 1000))


async def _ground(
    query: EstimationQuery, request: EstimateFromTranscriptRequest
) -> tuple[GroundedEstimate, list[RetrievedChunk], int]:
    """Retrieve, assemble, generate and validate (steps 3-6).

    Returns ``(estimate, chunks, validation_retries)`` where ``validation_retries``
    is 0 or 1. Raises ``CitationValidationError`` when the corrective retry did
    not fix the citations.
    """
    settings = get_settings()

    search_mode = request.search_mode or settings.RETRIEVAL_SEARCH_MODE
    rerank = settings.RERANKER_ENABLED if request.rerank is None else request.rerank

    # Lazy import: this module must stay importable without the retrieval extras,
    # and the pipeline resolves embedder/store only when it runs.
    from app.generation.rag.retrieval.pipeline import retrieve

    with log_stage(
        "retrieve", mode=search_mode, rerank=rerank, top_k=request.top_k
    ) as stage:
        result = await retrieve(
            query=query.search_query,
            top_k=request.top_k,
            recall_k=settings.RETRIEVAL_RECALL_TOP_K,
            search_mode=search_mode,
            rerank=rerank,
            rrf_k=settings.RRF_K,
            rerank_top_n=settings.RERANK_TOP_N,
            # The chain cares whether the corpus has an analogue at all. Without a
            # floor the vector branch always returns `k` chunks, so an
            # unrelated project would come back "grounded" on five irrelevant
            # budgets and a low confidence score nobody is obliged to read.
            distance_threshold=settings.RETRIEVAL_DISTANCE_THRESHOLD,
        )
        chunks = list(result.chunks)
        stage["retrieved"] = len(chunks)
        stage["low_confidence"] = result.low_confidence

    if not chunks:
        # Nothing retrieved: skip generation and validation entirely. ``assemble``
        # is skipped too — it rejects an empty list, and there is no context to
        # build.
        log.info("estimate_short_circuited", reason="no_chunks_retrieved")
        return unavailable_estimate(), chunks, 0

    from app.generation.rag.context_assembler import assemble, sources_of

    with log_stage("assemble", chunks=len(chunks)) as stage:
        context = assemble(chunks)
        allowed_ids = sources_of(chunks)
        stage["sources"] = len(allowed_ids)
        stage["context_chars"] = len(context)

    with log_stage("generate", sources=len(allowed_ids)):
        estimate = await generate_estimate(query, context, source_ids=allowed_ids)

    with log_stage("validate_citations", citations=len(estimate.citations)):
        check = check_citations(estimate, allowed_ids)

    if check.ok:
        return estimate, chunks, 0

    log.warning(
        "estimate_citations_invalid",
        unknown_ids=check.unknown_ids,
        message=check.message,
    )

    # Exactly one corrective retry: the invalid ids are named in the prompt, so
    # this is a directed fix rather than "try again". When the citation was
    # malformed (a non-integer ``chunk_id``) there is no id to name, so the
    # check's message travels as text — otherwise the retry would be blind.
    with log_stage(
        "generate_retry",
        invalid_ids=len(check.unknown_ids),
        malformed=not check.unknown_ids,
    ):
        retried = await generate_estimate(
            query,
            context,
            source_ids=allowed_ids,
            invalid_citation_ids=check.unknown_ids,
            invalid_citation_note=check.message if not check.unknown_ids else None,
        )

    with log_stage("validate_citations_retry", citations=len(retried.citations)):
        recheck = check_citations(retried, allowed_ids)

    if not recheck.ok:
        raise CitationValidationError(
            "Estimate still cites chunk ids outside the retrieved context after "
            f"one corrective retry: {recheck.unknown_ids or recheck.message}"
        )

    return retried, chunks, 1


async def estimate_from_transcript(
    request: EstimateFromTranscriptRequest,
) -> EstimateFromTranscriptResponse:
    """Run the full grounded-estimation chain for one transcript.

    Args:
        request: Validated body of ``POST /v1/estimate/from-transcript``.

    Returns:
        The estimate, plus the query, the retrieved chunks and the per-request
        observability fields (``retrieved``, ``cached``, ``validation_retries``,
        ``pipeline_ms``).

    Raises:
        ReformulationError: the transcript could not be interpreted (peer module).
        ContextAssemblyError: chunks could not be assembled into context (peer
            module; the empty-list case never reaches it).
        GenerationError: the model call failed.
        CitationValidationError: the estimate still cited unknown ids after the
            single corrective retry.
        RetrievalUnavailable: no embedder or no vector store is wired.
    """
    started = time.perf_counter()
    store = get_idempotency_store()
    cache_key = (
        make_key(request.idempotency_key, request.transcript)
        if request.idempotency_key
        else None
    )
    request_id = cache_key[:12] if cache_key else None

    if cache_key is not None:
        with log_stage("idempotency_lookup", request_id=request_id) as stage:
            cached = store.get(cache_key)
            stage["hit"] = cached is not None
        if cached is not None:
            # The stored response was served fresh once; this caller is a replay.
            # ``pipeline_ms`` keeps the original run's wall clock: it describes
            # how long the estimate took to produce, not how fast the cache is.
            return cached.model_copy(update={"cached": True})

    with log_stage(
        "reformulate", request_id=request_id, transcript_chars=len(request.transcript)
    ) as stage:
        # Lazy import: a peer owns this module; the chain only needs its signature.
        from app.generation.rag.query_reformulator import reformulate

        query = await reformulate(request.transcript)
        stage["query_source"] = query.source

    estimate, chunks, retries = await _ground(query, request)

    response = EstimateFromTranscriptResponse(
        estimate=estimate,
        query=query,
        chunks=chunks,
        retrieved=len(chunks),
        cached=False,
        validation_retries=retries,
        pipeline_ms=_elapsed_ms(started),
    )

    if cache_key is not None:
        # Stored after success only: a failed chain must be retried, not replayed.
        with log_stage("idempotency_store", request_id=request_id):
            store.put(cache_key, response)

    log.info(
        "estimate_completed",
        request_id=request_id,
        retrieved=len(chunks),
        grounded=estimate.grounded,
        validation_retries=retries,
        pipeline_ms=response.pipeline_ms,
    )
    return response
