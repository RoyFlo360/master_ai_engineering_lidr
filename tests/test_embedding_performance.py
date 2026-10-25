"""Performance tests for the embedding pipeline.

These are property assertions, not benchmarks: they pin the shape of the work
(one API call per 100 chunks, linear token growth) so an accidental N+1 or
quadratic path fails the suite. The two wall-clock guards are deliberately
generous; they catch pathological behaviour, never normal variance.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.embedder import BATCH_SIZE, OpenAIEmbedder
from app.embedding_pipeline.schemas import Budget, Chunk, EmbeddedChunk

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


def sample_budgets() -> list[Budget]:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    return [Budget.model_validate(budget) for budget in payload]


def make_chunks(count: int) -> list[Chunk]:
    return [
        Chunk(chunk_id=f"BUD::{n}", text=f"text {n}", metadata={}, token_count=10)
        for n in range(count)
    ]


class CountingEmbedderAPI:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, *, model: str, input: list[str]):  # noqa: A002 - API name
        self.calls += 1
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.0, 0.0]) for _ in input]
        )


class CountingClient:
    def __init__(self) -> None:
        self.embeddings = CountingEmbedderAPI()


def test_api_calls_are_ceil_chunks_over_batch_size() -> None:
    for count in (250, 1000):
        client = CountingClient()
        OpenAIEmbedder(client).embed_many(make_chunks(count))
        assert client.embeddings.calls == math.ceil(count / BATCH_SIZE)


def test_embedding_work_is_linear_in_chunk_count() -> None:
    """Doubling the chunks doubles the API calls, not more."""
    small = CountingClient()
    large = CountingClient()
    OpenAIEmbedder(small).embed_many(make_chunks(200))
    OpenAIEmbedder(large).embed_many(make_chunks(400))
    assert large.embeddings.calls == 2 * small.embeddings.calls


def test_token_totals_are_linear_when_the_corpus_doubles() -> None:
    chunker = JSONStructuralChunker()
    budgets = sample_budgets()
    duplicated = [
        budget.model_copy(update={"budget_id": f"{budget.budget_id}-COPY"})
        for budget in budgets
    ]
    single = sum(chunk.token_count for chunk in chunker.chunk(budgets))
    double = sum(chunk.token_count for chunk in chunker.chunk(budgets + duplicated))
    assert double == 2 * single


def test_chunking_the_sample_is_fast() -> None:
    chunker = JSONStructuralChunker()
    budgets = sample_budgets()
    chunker.chunk(budgets)  # warm the tokenizer cache

    started = time.perf_counter()
    chunks = chunker.chunk(budgets)
    elapsed = time.perf_counter() - started

    assert len(chunks) == 60
    assert elapsed < 2.0, f"chunking 60 components took {elapsed:.2f}s"


def test_assembling_a_large_response_tail_is_fast() -> None:
    """500 embedded chunks is the request cap; assembly must not be quadratic."""
    chunks = make_chunks(500)
    started = time.perf_counter()
    embedded = [
        EmbeddedChunk(**chunk.model_dump(), embedding=[0.1] * 8) for chunk in chunks
    ]
    elapsed = time.perf_counter() - started

    assert len(embedded) == 500
    assert elapsed < 2.0, f"assembling 500 chunks took {elapsed:.2f}s"
