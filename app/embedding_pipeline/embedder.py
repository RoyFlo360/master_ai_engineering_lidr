"""OpenAI embedder.

Wraps ``text-embedding-3-small``. ``embed_many`` batches chunks into one API
call per batch (the embeddings endpoint accepts a list of inputs), which is far
cheaper in latency than one request per chunk.

Only rate limits are retried. Any other failure propagates: a malformed request
or a bad key will not improve by waiting.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from openai import RateLimitError

from app.embedding_pipeline.schemas import Chunk, EmbeddedChunk

log = structlog.get_logger(__name__)

# Default model for this pipeline. The full 1536-dim output is kept; Matryoshka
# dimension trimming is a live-session discussion, not part of this exercise.
MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Chunks per API call. The endpoint accepts lists; batching avoids hammering the
# API with serialized single-item requests.
BATCH_SIZE = 100

# Pricing constant — CHANGES OVER TIME. As of this exercise,
# text-embedding-3-small is $0.02 per 1M input tokens. This is deliberately
# separate from ``llm_wrapper.MODEL_COSTS`` (chat completions), which has no
# embedding entries.
PRICE_PER_MILLION_TOKENS_USD = 0.02

# Retry schedule (seconds) for rate limits: three retries after the first try.
_RETRY_BACKOFF_SECONDS = (1, 2, 4)


class EmbeddingCountMismatchError(RuntimeError):
    """The API returned a different number of vectors than it was given texts."""


def estimated_cost_usd(total_tokens: int) -> float:
    """Cost in USD for embedding ``total_tokens`` input tokens."""
    return total_tokens / 1_000_000 * PRICE_PER_MILLION_TOKENS_USD


class OpenAIEmbedder:
    """Thin wrapper over ``client.embeddings.create`` with batching and retries."""

    def __init__(self, client: Any, model: str = MODEL) -> None:
        self._client = client
        self._model = model

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Used by the ``compare.py`` CLI."""
        return self._create([text])[0]

    def embed_many(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        """Embed every chunk in order, batching the API calls."""
        embedded: list[EmbeddedChunk] = []
        for start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[start : start + BATCH_SIZE]
            texts = [chunk.text for chunk in batch]
            batch_tokens = sum(chunk.token_count for chunk in batch)

            started = time.perf_counter()
            vectors = self._create(texts)
            latency_ms = round((time.perf_counter() - started) * 1000, 1)

            if len(vectors) != len(batch):
                raise EmbeddingCountMismatchError(
                    f"embedding API returned {len(vectors)} vectors for "
                    f"{len(batch)} texts; refusing to attach vectors by position"
                )

            log.info(
                "embedding_batch_done",
                chunks=len(batch),
                tokens=batch_tokens,
                latency_ms=latency_ms,
                model=self._model,
            )

            for chunk, vector in zip(batch, vectors):
                embedded.append(EmbeddedChunk(**chunk.model_dump(), embedding=vector))
        return embedded

    def _create(self, texts: list[str]) -> list[list[float]]:
        """Call the embeddings API, retrying rate limits with exponential backoff."""
        last_error: RateLimitError | None = None
        for attempt, wait in enumerate((0, *_RETRY_BACKOFF_SECONDS)):
            if wait:
                time.sleep(wait)
            try:
                response = self._client.embeddings.create(
                    model=self._model, input=texts
                )
                return [item.embedding for item in response.data]
            except RateLimitError as exc:
                last_error = exc
                if attempt < len(_RETRY_BACKOFF_SECONDS):
                    log.warning(
                        "embedding_rate_limited",
                        attempt=attempt + 1,
                        retry_in_s=_RETRY_BACKOFF_SECONDS[attempt],
                    )
        assert last_error is not None, "loop only exits after a caught error"
        raise last_error
