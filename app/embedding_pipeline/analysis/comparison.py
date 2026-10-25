"""Chunking comparison framework.

Two informal signals (formal recall@k / NDCG need labelled relevance judgments
and are out of scope):

1. **Corpus stats** per strategy — how many chunks, the token distribution, and
   how many degenerate (orphan < 20 tok, obese > 800 tok). A glance tells you
   which strategy decomposes sanely and which one falls apart.
2. **Top-k cosine** over a small fixed query set — for each query, the best
   chunks each strategy retrieves, with their similarity, ready to read on screen
   and judge by eye (binary relevance).

Nothing is persisted; everything lives in memory.

Each strategy is chunked **once** per ``budgets`` object and the result memoized,
so asking for both stats and queries never doubles the expensive LLM calls of
strategies like propositional or contextual_retrieval.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from app.embedding_pipeline.analysis.similarity import cosine_similarity, percentile
from app.embedding_pipeline.chunking.base import Chunker
from app.embedding_pipeline.embedder import OpenAIEmbedder
from app.embedding_pipeline.schemas import Budget, Chunk

# Degeneracy thresholds (tokens). Clearly labelled — the only knobs.
ORPHAN_MAX_TOKENS = 20
OBESE_MIN_TOKENS = 800


class TokenDistribution(BaseModel):
    min: int = 0
    p50: float = 0.0
    p95: float = 0.0
    max: int = 0


class ChunkingStats(BaseModel):
    strategy: str
    n_chunks: int
    token_distribution: TokenDistribution
    n_orphan_chunks: int
    n_obese_chunks: int
    ingestion_cost_usd: float
    ingestion_seconds: float


class TopChunk(BaseModel):
    chunk_id: str
    cosine: float
    text_preview: str


class QueryResult(BaseModel):
    strategy: str
    query: str
    top_k: list[TopChunk] = Field(default_factory=list)


class CompareRequest(BaseModel):
    """Payload for ``POST /embeddings/compare``."""

    model_config = {"extra": "forbid"}

    budgets: list[Budget] = Field(min_length=1, max_length=100)
    queries: list[str] = Field(default_factory=list, max_length=20)
    # Empty = compare every registered strategy.
    strategies: list[str] = Field(default_factory=list, max_length=8)
    top_k: int = Field(default=3, ge=1, le=10)


class CompareResponse(BaseModel):
    """Response for ``POST /embeddings/compare``: stats and top-k per strategy."""

    stats_per_strategy: dict[str, ChunkingStats]
    queries_per_strategy: dict[str, list[QueryResult]]


def preview(text: str, limit: int = 160) -> str:
    """One-line preview, flattened and truncated with an ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class ChunkingComparator:
    """Runs a set of chunkers over the same corpus and compares them."""

    def __init__(self, chunkers: list[Chunker], embedder: OpenAIEmbedder) -> None:
        self._chunkers = chunkers
        self._embedder = embedder
        self._materialized: dict[str, dict] | None = None
        self._materialized_for: list[Budget] | None = None

    def _materialize(self, budgets: list[Budget]) -> dict[str, dict]:
        """Chunk every strategy once; memoize per ``budgets`` object identity."""
        if self._materialized is not None and self._materialized_for is budgets:
            return self._materialized
        out: dict[str, dict] = {}
        for chunker in self._chunkers:
            started = time.perf_counter()
            chunks = chunker.chunk(budgets)
            out[chunker.strategy_name] = {
                "chunks": chunks,
                "elapsed": time.perf_counter() - started,
                "cost": chunker.last_extra_cost_usd,
            }
        self._materialized = out
        self._materialized_for = budgets
        return out

    def compute_stats(self, budgets: list[Budget]) -> dict[str, ChunkingStats]:
        """Corpus statistics per strategy (chunk count, token spread, degeneracy)."""
        materialized = self._materialize(budgets)
        return {
            name: self.stats_for(name, data["chunks"], data["elapsed"], data["cost"])
            for name, data in materialized.items()
        }

    def run_queries(
        self, budgets: list[Budget], queries: list[str], top_k: int = 3
    ) -> dict[str, list[QueryResult]]:
        """Top-``top_k`` chunks per query, per strategy."""
        if not queries:
            return {}
        materialized = self._materialize(budgets)
        query_vectors = {query: self._embedder.embed_one(query) for query in queries}
        results: dict[str, list[QueryResult]] = {}

        for name, data in materialized.items():
            embedded = self._embedder.embed_many(data["chunks"])
            per_query: list[QueryResult] = []
            for query in queries:
                query_vector = query_vectors[query]
                scored = sorted(
                    (
                        TopChunk(
                            chunk_id=chunk.chunk_id,
                            cosine=round(cosine_similarity(query_vector, chunk.embedding), 4),
                            text_preview=preview(chunk.text),
                        )
                        for chunk in embedded
                    ),
                    key=lambda top: top.cosine,
                    reverse=True,
                )
                per_query.append(
                    QueryResult(strategy=name, query=query, top_k=scored[:top_k])
                )
            results[name] = per_query
        return results

    @staticmethod
    def stats_for(name: str, chunks: list[Chunk], elapsed: float, cost: float) -> ChunkingStats:
        token_counts = [chunk.token_count for chunk in chunks]
        return ChunkingStats(
            strategy=name,
            n_chunks=len(chunks),
            token_distribution=TokenDistribution(
                min=min(token_counts) if token_counts else 0,
                p50=round(percentile(token_counts, 50), 1),
                p95=round(percentile(token_counts, 95), 1),
                max=max(token_counts) if token_counts else 0,
            ),
            n_orphan_chunks=sum(1 for count in token_counts if count < ORPHAN_MAX_TOKENS),
            n_obese_chunks=sum(1 for count in token_counts if count > OBESE_MIN_TOKENS),
            ingestion_cost_usd=round(cost, 6),
            ingestion_seconds=round(elapsed, 3),
        )
