"""Unit tests for the four retrieval configurations (Session 10, T5.1).

Hermetic: the embedder, store, session factory and reranker are fakes. No
Postgres, no OpenAI, no torch. The tests pin *which* work each configuration
does — one embedding, one vector query, one lexical query in hybrid mode, RRF
before rerank, ``recall_k`` into the reranker and ``top_n`` out of it — because
that shape is what the four-configuration comparison table claims to measure.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from app.generation.rag.retrieval.errors import (
    RerankerUnavailable,
    RetrievalUnavailable,
)
from app.generation.rag.retrieval.pipeline import retrieve

QUERY = "mobile banking API with OAuth 2.0"
QUERY_VECTOR = [0.01, 0.02, 0.03]


class FakeRow:
    """A row shaped like ``ChunkStore.search`` / ``search_lexical``.

    The store returns SQLAlchemy ``Row`` objects, so the fake exposes attributes,
    ``row[key]`` and ``row._mapping`` — the three ways the pipeline may read it.
    """

    def __init__(
        self,
        chunk_id: int,
        *,
        distance: float | None = None,
        rank: float | None = None,
        content: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.chunk_id = chunk_id
        self.document_id = 1000 + chunk_id
        self.chunk_type = "budget_component"
        self.content = content or f"chunk content {chunk_id}"
        self.metadata = metadata or {
            "budget_id": f"BUD-{chunk_id:04d}",
            "component_id": f"C-{chunk_id}",
            "sector": "finance",
            "project_year": 2024,
        }
        self.distance = distance
        self.rank = rank
        self._mapping = {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "chunk_type": self.chunk_type,
            "content": self.content,
            "metadata": self.metadata,
            "distance": self.distance,
            "rank": self.rank,
        }

    def __getitem__(self, key):
        return self._mapping[key]

    def keys(self):
        return list(self._mapping)


class FakeSession:
    """Opaque session token; the fake store never reads it."""


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


class FakeEmbedder:
    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = list(vector or QUERY_VECTOR)
        self.calls: list[str] = []

    def embed_one(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self.vector)


class FakeStore:
    def __init__(
        self,
        vector_rows: list[FakeRow] | None = None,
        lexical_rows: list[FakeRow] | None = None,
    ) -> None:
        self.vector_rows = list(vector_rows or [])
        self.lexical_rows = list(lexical_rows or [])
        self.search_calls: list[dict] = []
        self.lexical_calls: list[dict] = []

    async def search(
        self,
        session: FakeSession,
        *,
        query_vector: list[float],
        k: int,
        distance_threshold: float | None = None,
    ) -> list[FakeRow]:
        self.search_calls.append(
            {
                "session": session,
                "query_vector": list(query_vector),
                "k": k,
                "distance_threshold": distance_threshold,
            }
        )
        rows = self.vector_rows
        if distance_threshold is not None:
            rows = [
                row
                for row in rows
                if row.distance is None or row.distance <= distance_threshold
            ]
        return rows[:k]

    async def search_lexical(
        self,
        session: FakeSession,
        *,
        query_text: str,
        top_k: int = 50,
    ) -> list[FakeRow]:
        self.lexical_calls.append(
            {"session": session, "query_text": query_text, "top_k": top_k}
        )
        return self.lexical_rows[:top_k]


class FakeReranker:
    """Reverses the candidate list so "reranked" is distinguishable from "not"."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def rerank(
        self,
        query: str,
        candidates,
        *,
        top_n: int,
        text_of=lambda item: item.content,
    ) -> list:
        candidates = list(candidates)
        self.calls.append(
            {
                "query": query,
                "candidates": candidates,
                "top_n": top_n,
                "text_of": text_of,
            }
        )
        if self.error is not None:
            raise self.error
        return list(reversed(candidates))[:top_n]


def run_retrieve(**kwargs):
    return asyncio.run(retrieve(**kwargs))


@pytest.fixture(autouse=True)
def no_real_reranker(monkeypatch):
    """Never let this file reach a real cross-encoder.

    ``retrieve`` resolves an omitted ``reranker`` from ``app.dependencies``, so
    any call that ends up with ``rerank=True`` — including a mutated default —
    would otherwise try to load torch and download weights. That turned such a
    mutant into a hang rather than a failure, which reads as "timeout" instead
    of "killed". Patching the factory keeps every call in this file hermetic.
    """
    import app.dependencies as dependencies

    monkeypatch.setattr(dependencies, "get_reranker", lambda: FakeReranker())


class RecordingLogger:
    """Stand-in for the module's structlog logger.

    ``capture_logs()`` depends on structlog's global configuration, which the
    application may replace or cache before a test runs; recording the calls on
    a fake is both deterministic and what ``rules/testing.md`` prefers.
    """

    def __init__(self) -> None:
        self.errors: list[tuple[str | None, dict]] = []
        self.infos: list[tuple[str | None, dict]] = []

    def error(self, event: str | None = None, **kwargs) -> None:
        self.errors.append((event, kwargs))

    def info(self, event: str | None = None, **kwargs) -> None:
        self.infos.append((event, kwargs))


def expected_rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Independent oracle for the fused order (fusion itself is tested elsewhere)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        seen: set[int] = set()
        for position, chunk_id in enumerate(ranking, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position)
    return [chunk_id for chunk_id, _ in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))]


def build(
    vector_rows: list[FakeRow] | None = None,
    lexical_rows: list[FakeRow] | None = None,
) -> tuple[FakeEmbedder, FakeStore, FakeSessionFactory]:
    return FakeEmbedder(), FakeStore(vector_rows, lexical_rows), FakeSessionFactory()


# --- configuration A: vector, no rerank ------------------------------------


def test_vector_without_rerank_returns_distance_order() -> None:
    rows = [
        FakeRow(1, distance=0.10),
        FakeRow(2, distance=0.20),
        FakeRow(3, distance=0.30),
    ]
    embedder, store, factory = build(vector_rows=rows)

    result = run_retrieve(
        query=QUERY,
        top_k=3,
        search_mode="vector",
        rerank=False,
        embedder=embedder,
        store=store,
        session_factory=factory,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == [1, 2, 3]
    assert [chunk.distance for chunk in result.chunks] == pytest.approx([0.10, 0.20, 0.30])
    assert all(chunk.source == "vector" for chunk in result.chunks)
    assert result.search_mode == "vector"
    assert result.rerank is False
    assert result.candidates_evaluated == 3
    assert store.lexical_calls == []
    assert embedder.calls == [QUERY]
    assert store.search_calls[0]["query_vector"] == QUERY_VECTOR


# --- configuration B: hybrid, no rerank ------------------------------------


def test_hybrid_without_rerank_returns_rrf_order_and_labels_lexical_hits() -> None:
    vector_rows = [FakeRow(chunk_id, distance=chunk_id / 100) for chunk_id in (1, 2, 3, 4, 5)]
    lexical_rows = [
        FakeRow(chunk_id, rank=1.0 / position)
        for position, chunk_id in enumerate((5, 4, 3, 6, 7), start=1)
    ]
    embedder, store, factory = build(vector_rows, lexical_rows)

    result = run_retrieve(
        query=QUERY,
        top_k=7,
        recall_k=5,
        search_mode="hybrid",
        rerank=False,
        rrf_k=60,
        embedder=embedder,
        store=store,
        session_factory=factory,
    )

    expected = expected_rrf([[1, 2, 3, 4, 5], [5, 4, 3, 6, 7]], k=60)
    assert [chunk.chunk_id for chunk in result.chunks] == expected
    assert result.search_mode == "hybrid"
    assert result.rerank is False
    assert result.candidates_evaluated == len(vector_rows) + len(lexical_rows)

    by_id = {chunk.chunk_id: chunk for chunk in result.chunks}
    # A lexical-only hit has no cosine distance and says where it came from.
    assert by_id[6].distance is None
    assert by_id[6].source == "lexical"
    assert by_id[7].distance is None
    assert by_id[7].source == "lexical"
    # A vector hit keeps its distance.
    assert by_id[1].distance == pytest.approx(0.01)

    assert len(store.search_calls) == 1
    assert store.search_calls[0]["k"] == 5
    assert len(store.lexical_calls) == 1
    assert store.lexical_calls[0]["query_text"] == QUERY
    assert store.lexical_calls[0]["top_k"] == 5
    assert embedder.calls == [QUERY]


# --- configuration C: vector, rerank ---------------------------------------


def test_vector_with_rerank_passes_recall_k_candidates_and_returns_top_n() -> None:
    vector_rows = [FakeRow(chunk_id, distance=chunk_id / 10) for chunk_id in (1, 2, 3, 4)]
    embedder, store, factory = build(vector_rows=vector_rows)
    reranker = FakeReranker()

    result = run_retrieve(
        query=QUERY,
        top_k=3,
        recall_k=4,
        rerank=True,
        rerank_top_n=2,
        search_mode="vector",
        embedder=embedder,
        store=store,
        session_factory=factory,
        reranker=reranker,
    )

    assert len(reranker.calls) == 1
    call = reranker.calls[0]
    assert [chunk.chunk_id for chunk in call["candidates"]] == [1, 2, 3, 4]
    assert call["top_n"] == 2
    # The fake reverses; the pipeline must return its top_n, not the recall list.
    assert [chunk.chunk_id for chunk in result.chunks] == [4, 3]
    assert result.rerank is True
    assert result.candidates_evaluated == 4
    assert store.lexical_calls == []


# --- configuration D: hybrid, rerank ---------------------------------------


def test_hybrid_with_rerank_fuses_then_reranks() -> None:
    vector_rows = [FakeRow(chunk_id, distance=chunk_id / 10) for chunk_id in (1, 2, 3, 4)]
    lexical_rows = [FakeRow(chunk_id, rank=1.0 / position) for position, chunk_id in enumerate((4, 5, 6), start=1)]
    embedder, store, factory = build(vector_rows, lexical_rows)
    reranker = FakeReranker()

    result = run_retrieve(
        query=QUERY,
        top_k=3,
        recall_k=4,
        rerank=True,
        rerank_top_n=2,
        search_mode="hybrid",
        rrf_k=60,
        embedder=embedder,
        store=store,
        session_factory=factory,
        reranker=reranker,
    )

    fused = expected_rrf([[1, 2, 3, 4], [4, 5, 6]], k=60)
    assert len(reranker.calls) == 1
    call = reranker.calls[0]
    candidate_ids = [chunk.chunk_id for chunk in call["candidates"]]
    # At most recall_k reach the cross-encoder, in fused order.
    assert len(candidate_ids) <= 4
    assert candidate_ids == fused[: len(candidate_ids)]
    assert call["top_n"] == 2
    assert [chunk.chunk_id for chunk in result.chunks] == list(reversed(candidate_ids))[:2]
    assert result.search_mode == "hybrid"
    assert result.rerank is True
    assert result.candidates_evaluated == len(vector_rows) + len(lexical_rows)
    assert len(store.lexical_calls) == 1


# --- invariants shared by every configuration ------------------------------


def test_every_configuration_embeds_the_query_exactly_once() -> None:
    configurations = [
        {"search_mode": "vector", "rerank": False},
        {"search_mode": "hybrid", "rerank": False},
        {"search_mode": "vector", "rerank": True},
        {"search_mode": "hybrid", "rerank": True},
    ]

    for configuration in configurations:
        vector_rows = [FakeRow(1, distance=0.1), FakeRow(2, distance=0.2)]
        lexical_rows = [FakeRow(2, rank=0.9), FakeRow(3, rank=0.5)]
        embedder, store, factory = build(vector_rows, lexical_rows)

        run_retrieve(
            query=QUERY,
            top_k=2,
            recall_k=2,
            rrf_k=60,
            embedder=embedder,
            store=store,
            session_factory=factory,
            reranker=FakeReranker(),
            **configuration,
        )

        assert embedder.calls == [QUERY], configuration
        assert len(store.search_calls) == 1, configuration


def test_empty_result_is_low_confidence() -> None:
    embedder, store, factory = build()

    result = run_retrieve(
        query=QUERY,
        top_k=5,
        recall_k=5,
        search_mode="hybrid",
        rerank=False,
        embedder=embedder,
        store=store,
        session_factory=factory,
    )

    assert result.chunks == []
    assert result.low_confidence is True
    assert result.candidates_evaluated == 0
    assert result.search_time_ms >= 0
    assert embedder.calls == [QUERY]


def test_missing_embedder_raises_retrieval_unavailable(monkeypatch) -> None:
    import app.dependencies as dependencies

    monkeypatch.setattr(dependencies, "get_embedder", lambda: None)
    _, store, factory = build(vector_rows=[FakeRow(1, distance=0.1)])

    with pytest.raises(RetrievalUnavailable):
        run_retrieve(
            query=QUERY,
            top_k=3,
            search_mode="vector",
            rerank=False,
            embedder=None,
            store=store,
            session_factory=factory,
        )

    assert store.search_calls == []


def test_reranker_failure_propagates_and_never_falls_back() -> None:
    _, store, factory = build(vector_rows=[FakeRow(1, distance=0.1)])
    reranker = FakeReranker(error=RerankerUnavailable("model exploded"))

    with pytest.raises(RerankerUnavailable):
        run_retrieve(
            query=QUERY,
            top_k=1,
            recall_k=1,
            search_mode="vector",
            rerank=True,
            embedder=FakeEmbedder(),
            store=store,
            session_factory=factory,
            reranker=reranker,
        )


def test_a_reranker_failure_carries_the_documented_public_message() -> None:
    """``retrieve`` raises; the router turns this exact text into the 502 body."""
    _, store, factory = build(vector_rows=[FakeRow(1, distance=0.1)])

    with pytest.raises(RerankerUnavailable) as raised:
        run_retrieve(
            query=QUERY,
            top_k=1,
            recall_k=1,
            rerank=True,
            embedder=FakeEmbedder(),
            store=store,
            session_factory=factory,
            reranker=FakeReranker(error=RuntimeError("boom")),
        )

    assert str(raised.value) == "Reranking service is unavailable. Please retry later."


def test_an_unwired_store_carries_the_documented_public_message(monkeypatch) -> None:
    """The router publishes this text as the 500 body; a different string is a bug."""
    import app.dependencies as dependencies

    monkeypatch.setattr(dependencies, "get_chunk_store", lambda: None)
    monkeypatch.setattr(dependencies, "get_async_session_factory", lambda: object())

    with pytest.raises(RetrievalUnavailable) as raised:
        run_retrieve(query=QUERY, embedder=FakeEmbedder(), session_factory=None)

    assert str(raised.value) == "Embedding service is not available."


# --- defaults, injection and timing ---------------------------------------


def test_defaults_match_the_documented_contract() -> None:
    """The signature defaults are the surface ``Settings`` mirrors.

    Called without arguments beyond the query, the pipeline must fetch ``top_k``
    (10) candidates, not rerank, not go hybrid, and not touch the lexical branch.
    """
    rows = [FakeRow(chunk_id, distance=chunk_id / 100) for chunk_id in range(1, 21)]
    embedder, store, factory = build(vector_rows=rows)

    result = run_retrieve(
        query=QUERY,
        embedder=embedder,
        store=store,
        session_factory=factory,
    )

    assert store.search_calls[0]["k"] == 10, "default top_k is 10"
    assert result.search_mode == "vector"
    assert result.rerank is False
    assert store.lexical_calls == []
    assert len(result.chunks) == 10


def test_the_signature_defaults_are_the_documented_ones() -> None:
    """Asserted on ``inspect``, not on behaviour.

    ``search_mode`` only ever matters as ``== "hybrid"``, so a default of
    ``"VECTOR"`` (or any other spelling) silently behaves like ``"vector"`` and
    no call-order assertion can tell the difference. The default *is* the
    contract — ``Settings`` mirrors these values — so it is checked directly.
    """
    defaults = inspect.signature(retrieve).parameters

    assert defaults["top_k"].default == 10
    assert defaults["recall_k"].default == 50
    assert defaults["search_mode"].default == "vector"
    assert defaults["rerank"].default is False
    assert defaults["rrf_k"].default == 60
    assert defaults["rerank_top_n"].default == 5
    assert defaults["distance_threshold"].default is None


def test_the_default_rrf_k_reaches_the_fusion() -> None:
    """Hybrid without an explicit ``rrf_k`` fuses at 60, the documented constant."""
    import app.generation.rag.retrieval.pipeline as pipeline_module

    seen: list[int] = []
    original = pipeline_module.reciprocal_rank_fusion

    def recording_fusion(rankings, *, k=60):
        seen.append(k)
        return original(rankings, k=k)

    rows = [FakeRow(chunk_id, distance=chunk_id / 10) for chunk_id in (1, 2)]
    embedder, store, factory = build(vector_rows=rows, lexical_rows=[])

    pipeline_module.reciprocal_rank_fusion = recording_fusion
    try:
        run_retrieve(
            query=QUERY,
            search_mode="hybrid",
            embedder=embedder,
            store=store,
            session_factory=factory,
        )
    finally:
        pipeline_module.reciprocal_rank_fusion = original

    assert seen == [60]


def test_default_recall_k_widens_the_hybrid_branch() -> None:
    rows = [FakeRow(chunk_id, distance=chunk_id / 100) for chunk_id in range(1, 60)]
    embedder, store, factory = build(vector_rows=rows)

    run_retrieve(
        query=QUERY,
        search_mode="hybrid",
        embedder=embedder,
        store=store,
        session_factory=factory,
    )

    assert store.search_calls[0]["k"] == 50, "default recall_k is 50"
    assert store.lexical_calls[0]["top_k"] == 50


def test_default_rerank_top_n_is_five() -> None:
    rows = [FakeRow(chunk_id, distance=chunk_id / 100) for chunk_id in range(1, 9)]
    embedder, store, factory = build(vector_rows=rows)
    reranker = FakeReranker()

    result = run_retrieve(
        query=QUERY,
        rerank=True,
        recall_k=8,
        embedder=embedder,
        store=store,
        session_factory=factory,
        reranker=reranker,
    )

    assert reranker.calls[0]["top_n"] == 5, "default rerank_top_n is 5"
    assert len(result.chunks) == 5


def test_the_configured_rrf_k_reaches_the_fusion() -> None:
    """``rrf_k`` is forwarded; dropping it silently reverts to the default 60."""
    import app.generation.rag.retrieval.pipeline as pipeline_module

    seen: list[int] = []
    original = pipeline_module.reciprocal_rank_fusion

    def recording_fusion(rankings, *, k=60):
        seen.append(k)
        return original(rankings, k=k)

    rows = [FakeRow(chunk_id, distance=chunk_id / 10) for chunk_id in (1, 2)]
    embedder, store, factory = build(vector_rows=rows, lexical_rows=[])

    pipeline_module.reciprocal_rank_fusion = recording_fusion
    try:                             
        run_retrieve(
            query=QUERY,
            search_mode="hybrid",
            rrf_k=7,
            embedder=embedder,
            store=store,
            session_factory=factory,
        )
    finally:
        pipeline_module.reciprocal_rank_fusion = original

    assert seen == [7]


def test_the_request_scoped_session_is_the_one_both_branches_query() -> None:
    """The session from the caller's factory must reach the store, not ``None``."""
    rows = [FakeRow(1, distance=0.1), FakeRow(2, rank=0.9)]
    embedder, store, factory = build(vector_rows=rows, lexical_rows=[FakeRow(2, rank=0.9)])

    run_retrieve(
        query=QUERY,
        search_mode="hybrid",
        embedder=embedder,
        store=store,
        session_factory=factory,
    )

    assert factory.opened == 1
    assert isinstance(store.search_calls[0]["session"], FakeSession)
    assert isinstance(store.lexical_calls[0]["session"], FakeSession)


def test_an_injected_session_context_is_used_without_calling_it() -> None:
    """A caller that already holds a context manager passes it directly."""
    embedder, store, _factory = build(vector_rows=[FakeRow(1, distance=0.1)])
    context = _SessionContext()

    run_retrieve(
        query=QUERY,
        embedder=embedder,
        store=store,
        session_factory=context,
    )

    assert isinstance(store.search_calls[0]["session"], FakeSession)


def test_the_reranker_is_resolved_from_dependencies_when_not_injected(monkeypatch) -> None:
    """The factory is the seam; returning ``None`` here would be a silent 500."""
    import app.dependencies as dependencies

    factory_reranker = FakeReranker()
    monkeypatch.setattr(dependencies, "get_reranker", lambda: factory_reranker)
    _, store, factory = build(vector_rows=[FakeRow(1, distance=0.1), FakeRow(2, distance=0.2)])

    result = run_retrieve(
        query=QUERY,
        rerank=True,
        recall_k=2,
        rerank_top_n=2,
        embedder=FakeEmbedder(),
        store=store,
        session_factory=factory,
        reranker=None,
    )

    assert len(factory_reranker.calls) == 1
    assert [chunk.chunk_id for chunk in result.chunks] == [2, 1]


def test_the_reported_search_time_is_derived_from_the_two_timestamps(monkeypatch) -> None:
    """``search_time_ms`` is ``(end - start) * 1000``, floored at zero.

    A deterministic clock pins the arithmetic: ``* 1001``, ``/ 1000`` and
    ``end + start`` all produce a plausible-looking positive integer in real
    use, so nothing else catches them.
    """
    import app.generation.rag.retrieval.pipeline as pipeline_module

    class FakeClock:
        def __init__(self, ticks: list[float]) -> None:
            self.ticks = list(ticks)

        def perf_counter(self) -> float:
            return self.ticks.pop(0)

    def retrieve_with_clock(ticks: list[float]) -> int:
        embedder, store, factory = build(vector_rows=[FakeRow(1, distance=0.1)])
        monkeypatch.setattr(pipeline_module, "time", FakeClock(ticks))
        return run_retrieve(
            query=QUERY,
            top_k=1,
            embedder=embedder,
            store=store,
            session_factory=factory,
        ).search_time_ms

    assert retrieve_with_clock([100.0, 101.0]) == 1000
    assert retrieve_with_clock([100.0, 100.25]) == 250
    assert retrieve_with_clock([100.0, 100.0]) == 0, "a subsample run reports 0, not 1"


def test_a_failed_rerank_is_logged_with_the_error_type(monkeypatch) -> None:
    """The operator-facing signal the spec promises: event name plus exception type."""
    import app.generation.rag.retrieval.pipeline as pipeline_module

    log = RecordingLogger()
    monkeypatch.setattr(pipeline_module, "log", log)
    _, store, factory = build(vector_rows=[FakeRow(1, distance=0.1)])

    with pytest.raises(RerankerUnavailable):
        run_retrieve(
            query=QUERY,
            top_k=1,
            recall_k=1,
            rerank=True,
            embedder=FakeEmbedder(),
            store=store,
            session_factory=factory,
            reranker=FakeReranker(error=RuntimeError("boom")),
        )

    assert log.errors == [("reranker_unavailable", {"error_type": "RuntimeError"})]
