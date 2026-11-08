"""Performance properties for fusion and the retrieval pipeline (T5.4).

Property assertions, not benchmarks: they pin the *shape* of the work so an
accidental N+1, a per-candidate embedding call, or an unbounded rerank input
fails the suite. Wall-clock guards are deliberately generous and only catch
pathological behaviour — never normal variance.
"""

from __future__ import annotations

import asyncio
import time

from app.generation.rag.retrieval.fusion import reciprocal_rank_fusion
from app.generation.rag.retrieval.pipeline import retrieve

QUERY = "hybrid retrieval performance probe"


class FakeRow:
    def __init__(self, chunk_id: int, *, distance: float | None = None, rank: float | None = None) -> None:
        self.chunk_id = chunk_id
        self.document_id = 1000 + chunk_id
        self.chunk_type = "budget_component"
        self.content = f"chunk content {chunk_id}"
        self.metadata = {"budget_id": f"BUD-{chunk_id:04d}", "component_id": f"C-{chunk_id}"}
        self.distance = distance
        self.rank = rank
        self._mapping = {
            "chunk_id": chunk_id,
            "document_id": self.document_id,
            "chunk_type": self.chunk_type,
            "content": self.content,
            "metadata": self.metadata,
            "distance": distance,
            "rank": rank,
        }

    def __getitem__(self, key):
        return self._mapping[key]

    def keys(self):
        return list(self._mapping)


class FakeSession:
    pass


class _SessionContext:
    async def __aenter__(self) -> FakeSession:
        return FakeSession()

    async def __aexit__(self, *_exc) -> bool:
        return False


class FakeSessionFactory:
    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> _SessionContext:
        self.opened += 1
        return _SessionContext()


class CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        return [0.1, 0.2, 0.3]


class RecordingStore:
    def __init__(self, *, vector_count: int = 0, lexical_count: int = 0) -> None:
        self.vector_rows = [
            FakeRow(chunk_id, distance=chunk_id / 1000) for chunk_id in range(1, vector_count + 1)
        ]
        self.lexical_rows = [
            FakeRow(chunk_id, rank=1.0 / (position + 1))
            for position, chunk_id in enumerate(
                range(vector_count + 1, vector_count + lexical_count + 1)
            )
        ]
        self.search_calls = 0
        self.lexical_calls = 0

    async def search(self, session, *, query_vector, k, distance_threshold=None):
        self.search_calls += 1
        return self.vector_rows[:k]

    async def search_lexical(self, session, *, query_text, top_k=50):
        self.lexical_calls += 1
        return self.lexical_rows[:top_k]


class RecordingReranker:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def rerank(self, query, candidates, *, top_n, text_of=lambda item: item.content):
        candidates = list(candidates)
        self.calls.append(len(candidates))
        return candidates[:top_n]


def run_retrieve(**kwargs):
    return asyncio.run(retrieve(**kwargs))


def test_fusion_finishes_over_five_thousand_ids() -> None:
    ids = list(range(5_000))
    rankings = [ids, list(reversed(ids))]

    started = time.perf_counter()
    fused = reciprocal_rank_fusion(rankings, k=60)
    elapsed = time.perf_counter() - started

    assert len(fused) == 5_000
    # Generous guard: catches a pathological implementation, not a slow laptop.
    assert elapsed < 5.0, f"RRF over 5,000 ids took {elapsed:.2f}s"


def test_fusion_handles_ten_thousand_ids_within_a_generous_ceiling() -> None:
    """Doubling the input must not turn into an unusable call."""
    ids = list(range(10_000))
    rankings = [ids, list(reversed(ids))]

    started = time.perf_counter()
    fused = reciprocal_rank_fusion(rankings, k=60)
    elapsed = time.perf_counter() - started

    assert len(fused) == 10_000
    assert elapsed < 10.0, f"RRF over 10,000 ids took {elapsed:.2f}s"


def test_each_configuration_embeds_the_query_exactly_once() -> None:
    for search_mode, rerank in (
        ("vector", False),
        ("hybrid", False),
        ("vector", True),
        ("hybrid", True),
    ):
        embedder = CountingEmbedder()
        store = RecordingStore(vector_count=5, lexical_count=5)
        reranker = RecordingReranker()

        run_retrieve(
            query=QUERY,
            top_k=3,
            recall_k=5,
            search_mode=search_mode,
            rerank=rerank,
            embedder=embedder,
            store=store,
            session_factory=FakeSessionFactory(),
            reranker=reranker,
        )

        assert embedder.calls == 1, (search_mode, rerank)


def test_reranker_receives_at_most_recall_k_candidates() -> None:
    store = RecordingStore(vector_count=10, lexical_count=10)
    reranker = RecordingReranker()

    run_retrieve(
        query=QUERY,
        top_k=5,
        recall_k=10,
        search_mode="hybrid",
        rerank=True,
        embedder=CountingEmbedder(),
        store=store,
        session_factory=FakeSessionFactory(),
        reranker=reranker,
    )

    assert reranker.calls, "the reranker was never called in hybrid/rerank mode"
    assert max(reranker.calls) <= 10, (
        "reranker input must be bounded by recall_k, not by the union of branches: "
        f"{reranker.calls!r}"
    )


def test_vector_rerank_sends_exactly_recall_k_candidates() -> None:
    store = RecordingStore(vector_count=10)
    reranker = RecordingReranker()

    run_retrieve(
        query=QUERY,
        top_k=3,
        recall_k=10,
        search_mode="vector",
        rerank=True,
        embedder=CountingEmbedder(),
        store=store,
        session_factory=FakeSessionFactory(),
        reranker=reranker,
    )

    assert reranker.calls == [10]


def test_the_lexical_branch_runs_once_in_hybrid_and_never_in_vector_mode() -> None:
    for search_mode, expected in (("vector", 0), ("hybrid", 1)):
        store = RecordingStore(vector_count=3, lexical_count=3)

        run_retrieve(
            query=QUERY,
            top_k=3,
            recall_k=3,
            search_mode=search_mode,
            rerank=False,
            embedder=CountingEmbedder(),
            store=store,
            session_factory=FakeSessionFactory(),
        )

        assert store.search_calls == 1, search_mode
        assert store.lexical_calls == expected, search_mode
