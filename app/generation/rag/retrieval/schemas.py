"""Wire and internal contracts for query-time retrieval (Session 10).

``RetrievedChunk`` is the contract the fusion step, the reranker and a future
estimator all understand. ``source`` exists because ``distance`` alone cannot
say where a hit came from — a lexical-only hit has no distance at all, and the
pre-Session-10 shape had no way to express that without a sentinel value.

``RetrievalRequest`` is the body of ``POST /v1/retrieval/search``; every field
that has a ``Settings`` counterpart is optional so the route can distinguish
"the client asked for this" from "use the deployment default".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.embedding_pipeline.schemas import MAX_QUERY_CHARS, MAX_SEARCH_K

#: Maximum width of one branch before fusion/reranking. Wider than
#: ``MAX_SEARCH_K`` on purpose: recall is not the same budget as the reply.
MAX_RECALL_K = 200

Source = Literal["vector", "lexical", "fused"]
SearchMode = Literal["vector", "hybrid"]


class RetrievedChunk(BaseModel):
    """One ranked chunk, with its provenance and score carry-over."""

    chunk_id: int
    document_id: int
    budget_id: str | None = None
    component_id: str | None = None
    chunk_type: str
    content: str
    sector: str = "unknown"
    project_year: int = 0
    # ``None`` means "no distance exists for this hit" (lexical-only), never a
    # sentinel value. A fused/reranked survivor keeps its vector distance when it
    # had one.
    distance: float | None = None
    # ``ts_rank_cd`` from the lexical branch; ``None`` for vector-only hits.
    rank: float | None = None
    source: Source = "vector"


class RetrievalResult(BaseModel):
    """Response of ``POST /v1/retrieval/search`` and return of ``retrieve()``."""

    chunks: list[RetrievedChunk]
    search_mode: SearchMode
    rerank: bool
    # Vector rows + lexical rows, counted before deduplication. A fused list of
    # five can therefore report a hundred candidates evaluated; that is the
    # point — it says how much work the configuration actually did.
    candidates_evaluated: int = Field(ge=0)
    search_time_ms: int = Field(ge=0)
    low_confidence: bool = False


class RetrievalRequest(BaseModel):
    """Body of ``POST /v1/retrieval/search``.

    ``None`` means "not specified"; the router substitutes the deployment
    default from ``Settings``. A bad value fails here with a 422, before any
    embedding call is billed.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="Free-text query or raw transcript.",
    )
    top_k: int = Field(
        default=10, ge=1, le=MAX_SEARCH_K, description="Chunks returned to the caller."
    )
    recall_k: int = Field(
        default=50,
        ge=1,
        le=MAX_RECALL_K,
        description="Candidates fetched per branch before fusion/reranking.",
    )
    search_mode: SearchMode | None = Field(
        default=None, description="Overrides RETRIEVAL_SEARCH_MODE for this call."
    )
    rerank: bool | None = Field(
        default=None, description="Overrides RERANKER_ENABLED for this call."
    )
    rrf_k: int | None = Field(
        default=None, ge=1, le=200, description="RRF smoothing constant; overrides RRF_K."
    )
    distance_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Relevance floor on cosine distance for the vector branch.",
    )
