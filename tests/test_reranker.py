"""Unit tests for the cross-encoder reranker wrapper (Session 10, T5.1).

Hermetic: the model is a fake object installed on the instance, so no ``torch``
import, no weight download, no network. Ordering, ``top_n``, ``text_of``,
stability and the fail-closed error contract are the properties that matter; the
model's absolute score values are irrelevant.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.generation.rag.retrieval.errors import RerankerUnavailable
from app.generation.rag.retrieval.reranker import CrossEncoderReranker

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeCrossEncoder:
    """Stand-in for ``sentence_transformers.CrossEncoder``.

    Only ``predict(pairs) -> list[float]`` is used by the wrapper. The fake
    records the pairs it was given so ``text_of`` and input order are provable.
    """

    def __init__(
        self, scores: list[float] | None = None, *, error: Exception | None = None
    ) -> None:
        self.scores = list(scores or [])
        self.error = error
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs):
        recorded = [tuple(pair) for pair in pairs]
        self.calls.append(recorded)
        if self.error is not None:
            raise self.error
        assert len(recorded) == len(self.scores), (
            f"fake model got {len(recorded)} pairs for {len(self.scores)} scores"
        )
        return list(self.scores)


def install(reranker: CrossEncoderReranker, model: FakeCrossEncoder) -> CrossEncoderReranker:
    """Attach a fake model without importing torch or hitting the network.

    Two seams, both documented by the class: the instance slot the lazy loader
    fills, and the loader method itself. Installing both means the fake is used
    whether the wrapper short-circuits on the slot or always goes through the
    loader.
    """
    reranker._model = model                                               # noqa: SLF001
    if hasattr(reranker, "_ensure_loaded"):
        reranker._ensure_loaded = lambda: model                            # noqa: SLF001
    return reranker


def make_reranker(
    scores: list[float] | None = None, *, error: Exception | None = None
) -> tuple[CrossEncoderReranker, FakeCrossEncoder]:
    fake = FakeCrossEncoder(scores, error=error)
    reranker = install(CrossEncoderReranker("fake-cross-encoder"), fake)
    return reranker, fake


def candidate(chunk_id: int, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(chunk_id=chunk_id, content=text or f"document {chunk_id}")


def test_rerank_sorts_descending_and_keeps_top_n() -> None:
    reranker, fake = make_reranker([0.1, 0.9, 0.5])
    candidates = [candidate(1), candidate(2), candidate(3)]

    ranked = reranker.rerank("query", candidates, top_n=2)

    assert [chunk.chunk_id for chunk in ranked] == [2, 3]
    assert len(fake.calls) == 1
    # Pairs are (query, document) in input order, one batched call.
    assert fake.calls[0] == [
        ("query", "document 1"),
        ("query", "document 2"),
        ("query", "document 3"),
    ]


def test_score_of_no_documents_is_empty_without_touching_the_model() -> None:
    """An empty list must not trigger the model load (and its download)."""
    fake = FakeCrossEncoder([])
    reranker = install(CrossEncoderReranker("model-that-must-not-load"), fake)

    assert reranker.score("query", []) == []
    assert fake.calls == []


def test_rerank_with_no_candidates_is_empty() -> None:
    fake = FakeCrossEncoder([])
    reranker = install(CrossEncoderReranker("model-that-must-not-load"), fake)

    assert reranker.rerank("query", [], top_n=5) == []
    assert fake.calls == []


def test_text_of_is_honoured_for_a_non_content_shape() -> None:
    reranker, fake = make_reranker([0.2, 3.0, 1.0])
    candidates = [
        SimpleNamespace(chunk_id=1, text="alpha"),
        SimpleNamespace(chunk_id=2, text="beta"),
        SimpleNamespace(chunk_id=3, text="gamma"),
    ]

    ranked = reranker.rerank("query", candidates, top_n=3, text_of=lambda item: item.text)

    assert [chunk.chunk_id for chunk in ranked] == [2, 3, 1]
    assert fake.calls[0] == [("query", "alpha"), ("query", "beta"), ("query", "gamma")]


def test_equal_scores_keep_input_order() -> None:
    """The sort is stable; the first stage's order is the only tie-break."""
    reranker, _ = make_reranker([1.0, 1.0, 1.0])
    candidates = [candidate(3), candidate(1), candidate(2)]

    ranked = reranker.rerank("query", candidates, top_n=3)

    assert [chunk.chunk_id for chunk in ranked] == [3, 1, 2]


def test_top_n_larger_than_the_candidate_list_keeps_everything() -> None:
    reranker, _ = make_reranker([0.5, 0.1])
    candidates = [candidate(1), candidate(2)]

    ranked = reranker.rerank("query", candidates, top_n=10)

    assert [chunk.chunk_id for chunk in ranked] == [1, 2]


def test_failing_model_surfaces_reranker_unavailable() -> None:
    reranker, _ = make_reranker(error=RuntimeError("CUDA out of memory"))

    with pytest.raises(RerankerUnavailable):
        reranker.score("query", ["a", "b"])

    with pytest.raises(RerankerUnavailable):
        reranker.rerank("query", [candidate(1)], top_n=1)


def test_a_load_failure_surfaces_reranker_unavailable(monkeypatch) -> None:
    """The failure policy is fail-closed, so a broken install must not raise raw."""

    class BrokenCrossEncoder:
        def __init__(self, *args, **kwargs) -> None:
            raise OSError("model weights not found")

    for module_name in ("sentence_transformers", "transformers"):
        monkeypatch.setitem(
            sys.modules, module_name, SimpleNamespace(CrossEncoder=BrokenCrossEncoder)
        )

    reranker = CrossEncoderReranker("missing/model")

    with pytest.raises(RerankerUnavailable):
        reranker.load()


def test_importing_the_module_does_not_import_torch() -> None:
    """A fresh interpreter proves the parent's imports cannot mask a violation."""
    code = (
        "import sys\n"
        "import app.generation.rag.retrieval.reranker as module\n"
        "assert module\n"
        "banned = sorted({'torch', 'sentence_transformers', 'transformers'} & set(sys.modules))\n"
        "print(','.join(banned))\n"
        "raise SystemExit(1 if banned else 0)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, (
        "importing the reranker pulled a heavy runtime: "
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
