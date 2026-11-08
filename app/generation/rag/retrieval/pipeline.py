"""The four retrieval configurations, in one function (Session 10).

``retrieve`` is the only place that knows how vector search, lexical search,
fusion and reranking compose. The CLI evaluation harness, the HTTP route and the
tests all call it, so the numbers in the report and the numbers a client sees
cannot drift apart.

```
vector / no rerank : vector top_k
hybrid / no rerank : vector recall_k + lexical recall_k -> RRF -> top_k
vector / rerank    : vector recall_k -> reranker -> top_k
hybrid / rerank    : vector recall_k + lexical recall_k -> RRF -> reranker -> top_k
```

Dependencies are injectable and default to ``None``: an omitted argument is
resolved inside the function from ``app.dependencies``. That keeps the module
importable — and every test hermetic — without importing torch, OpenAI or a
database engine at module import time.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import structlog

from app.generation.rag.retrieval.errors import RerankerUnavailable, RetrievalUnavailable
from app.generation.rag.retrieval.fusion import reciprocal_rank_fusion
from app.generation.rag.retrieval.schemas import RetrievedChunk, RetrievalResult

log = structlog.get_logger(__name__)


def _field(row: Any, name: str) -> Any:
    """Read one column from a SQLAlchemy ``Row`` or a mapping-shaped fake."""
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _chunk_from_row(
    row: Any,
    *,
    source: str,
    distance: float | None = None,
    rank: float | None = None,
) -> RetrievedChunk:
    """Map one store row to the shared ``RetrievedChunk`` contract.

    ``budget_id``/``component_id``/``sector``/``project_year`` live in the JSONB
    ``metadata`` the structural chunker wrote (``client_sector`` and ``year`` are
    the stored key names). Missing keys degrade to the documented defaults
    instead of failing a whole retrieval on one sparse row.
    """
    metadata = _field(row, "metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}

    budget_id = metadata.get("budget_id")
    component_id = metadata.get("component_id")
    sector = metadata.get("client_sector") or metadata.get("sector") or "unknown"
    raw_year = metadata.get("year", metadata.get("project_year", 0)) or 0
    try:
        project_year = int(raw_year)
    except (TypeError, ValueError):
        project_year = 0

    return RetrievedChunk(
        chunk_id=int(_field(row, "chunk_id")),
        document_id=int(_field(row, "document_id")),
        budget_id=None if budget_id is None else str(budget_id),
        component_id=None if component_id is None else str(component_id),
        chunk_type=str(_field(row, "chunk_type") or "unknown"),
        content=str(_field(row, "content") or ""),
        sector=str(sector),
        project_year=project_year,
        distance=distance,
        rank=rank,
        source=source,
    )


def _survivors(reranked: Any) -> list[RetrievedChunk]:
    """Normalise the reranker's return value to a list of chunks.

    The cross-encoder returns the surviving candidates, best first. Accepting
    ``(candidate, score)`` pairs as well keeps this function independent of
    whether a reranker chooses to expose its scores.
    """
    survivors: list[RetrievedChunk] = []
    for item in reranked or []:
        candidate = item[0] if isinstance(item, (tuple, list)) else item
        if isinstance(candidate, RetrievedChunk):
            survivors.append(candidate)
    return survivors


def _resolve_services(embedder: Any, session_factory: Any, store: Any) -> tuple[Any, Any, Any]:
    """Fill omitted injectables from ``app.dependencies``.

    Imported inside the function so ``pipeline`` stays importable without the
    application's OpenAI/Redis side effects, and so tests can monkeypatch the
    factories without touching this module.
    """
    if embedder is not None and session_factory is not None and store is not None:
        return embedder, session_factory, store

    from app.dependencies import (
        get_async_session_factory,
        get_chunk_store,
        get_embedder,
    )

    if embedder is None:
        embedder = get_embedder()
    if session_factory is None:
        session_factory = get_async_session_factory()
    if store is None:
        store = get_chunk_store()
    return embedder, session_factory, store


async def retrieve(
    *,
    query: str,
    top_k: int = 10,
    recall_k: int = 50,
    search_mode: str = "vector",
    rerank: bool = False,
    rrf_k: int = 60,
    rerank_top_n: int = 5,
    distance_threshold: float | None = None,
    embedder: Any = None,
    session_factory: Any = None,
    store: Any = None,
    reranker: Any = None,
) -> RetrievalResult:
    """Run one of the four retrieval configurations and grade the result.

    Args:
        query: Free-text query, already validated by the caller.
        top_k: Chunks returned.
        recall_k: Candidates fetched per branch. Wider when reranking or fusing.
        search_mode: ``"vector"`` or ``"hybrid"``.
        rerank: Run the cross-encoder over the recalled candidates.
        rrf_k: RRF smoothing constant (positive).
        rerank_top_n: How many survivors the reranker keeps.
        distance_threshold: Optional cosine-distance floor on the vector branch.
        embedder, session_factory, store, reranker: Injectables. ``None`` resolves
            from ``app.dependencies`` (the four factories live there).
            ``session_factory`` may be a callable returning an async context
            manager (``async_sessionmaker``) or the context manager itself.

    Raises:
        RetrievalUnavailable: no embedder or no store — the service cannot run.
        RerankerUnavailable: the requested rerank step failed. Fail-closed: the
            caller never gets recall order labelled as reranked output.
    """
    started = time.perf_counter()

    embedder, session_factory, store = _resolve_services(embedder, session_factory, store)
    if embedder is None or store is None:
        # Same three deployment conditions as the pre-Session-10 /search route:
        # no OpenAI key, a non-Postgres URL, or a mismatched embedding model.
        raise RetrievalUnavailable("Embedding service is not available.")

    hybrid = search_mode == "hybrid"
    # Fusion and reranking both need a wider net than the reply; a plain vector
    # search can stop at top_k because nothing downstream reorders it.
    branch_limit = recall_k if (rerank or hybrid) else top_k

    # The OpenAI client is synchronous; one embed per request, off the loop.
    query_vector = await asyncio.to_thread(embedder.embed_one, query)

    search_kwargs: dict[str, Any] = {"query_vector": query_vector, "k": branch_limit}
    if distance_threshold is not None:
        # Optional so a store/fake written before Session 10 keeps working.
        search_kwargs["distance_threshold"] = distance_threshold

    # ``session_factory`` is normally ``async_sessionmaker`` (calling it yields an
    # async context manager). A caller that already holds one request-scoped
    # context — an HTTP dependency, a test's stand-in session — passes it
    # directly, so both shapes are accepted here rather than forcing every
    # collaborator to wrap itself.
    session_context = session_factory() if callable(session_factory) else session_factory

    async with session_context as session:
        vector_rows = list(await store.search(session, **search_kwargs))
        lexical_rows: list[Any] = []
        if hybrid:
            lexical_rows = list(
                await store.search_lexical(session, query_text=query, top_k=recall_k)
            )

    # One entry per chunk id. Vector rows are inserted first, so a chunk found by
    # both branches keeps its distance and is labelled "vector"; a lexical-only
    # hit has no distance at all (never a sentinel) and is labelled "lexical".
    by_id: dict[int, RetrievedChunk] = {}
    vector_ids: list[int] = []
    for row in vector_rows:
        chunk = _chunk_from_row(
            row, source="vector", distance=_as_float(_field(row, "distance"))
        )
        vector_ids.append(chunk.chunk_id)
        by_id.setdefault(chunk.chunk_id, chunk)

    lexical_ids: list[int] = []
    for row in lexical_rows:
        chunk = _chunk_from_row(
            row, source="lexical", rank=_as_float(_field(row, "rank"))
        )
        lexical_ids.append(chunk.chunk_id)
        by_id.setdefault(chunk.chunk_id, chunk)

    # A chunk both branches agree on is labelled "fused" — that is a statement
    # about provenance, not about ordering, so it is set here and never rewritten
    # by a later stage. Reranking only reorders; it must not claim a chunk came
    # from a fusion that never happened (vector+rerank has no lexical branch).
    shared = set(vector_ids) & set(lexical_ids)
    for chunk_id in shared:
        by_id[chunk_id] = by_id[chunk_id].model_copy(update={"source": "fused"})

    if hybrid:
        fused = reciprocal_rank_fusion([vector_ids, lexical_ids], k=rrf_k)
        ordered = [by_id[chunk_id] for chunk_id, _score in fused]
    else:
        ordered = [by_id[chunk_id] for chunk_id in vector_ids]

    if rerank:
        candidates = ordered[:recall_k]
        try:
            if reranker is None:
                from app.dependencies import get_reranker

                reranker = get_reranker()
            reranked = await asyncio.to_thread(
                reranker.rerank, query, candidates, top_n=rerank_top_n
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never fall back.
            log.error("reranker_unavailable", error_type=type(exc).__name__)
            raise RerankerUnavailable(
                "Reranking service is unavailable. Please retry later."
            ) from exc
        # Reranking reorders; it does not change where a chunk came from.
        ordered = _survivors(reranked)

    chunks = ordered[:top_k]
    elapsed_ms = int(max(0.0, (time.perf_counter() - started) * 1000))

    return RetrievalResult(
        chunks=chunks,
        search_mode="hybrid" if hybrid else "vector",
        rerank=bool(rerank),
        candidates_evaluated=len(vector_rows) + len(lexical_rows),
        search_time_ms=elapsed_ms,
        low_confidence=len(chunks) == 0,
    )
