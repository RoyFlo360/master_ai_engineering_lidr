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


class RecordingLogger:
    """Records ``log.info`` calls so the observability contract is provable.

    A fake rather than ``structlog.testing.capture_logs``: capture depends on
    structlog's global configuration, which the application may set or cache
    before the test runs.
    """

    def __init__(self) -> None:
        self.infos: list[tuple[str | None, dict]] = []
        self.errors: list[tuple[str | None, dict]] = []

    def info(self, event: str | None = None, **kwargs) -> None:
        self.infos.append((event, kwargs))

    def error(self, event: str | None = None, **kwargs) -> None:
        self.errors.append((event, kwargs))

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


def test_ensure_loaded_returns_a_resident_model_without_reloading() -> None:
    """The double-checked fast path: a loaded model is returned, not replaced.

    Every other test routes through ``install()``, which stubs ``_ensure_loaded``
    out — so the fast path itself was never executed and a mutation that inverted
    the ``is not None`` check survived.
    """
    sentinel = FakeCrossEncoder([])
    reranker = CrossEncoderReranker("already-loaded")
    reranker._model = sentinel                                        # noqa: SLF001

    assert reranker._ensure_loaded() is sentinel                       # noqa: SLF001
    assert reranker._model is sentinel                                 # noqa: SLF001


def test_top_n_zero_yields_no_survivors_and_touches_no_model() -> None:
    """``top_n <= 0`` is an empty result, never a Python negative-slice surprise."""
    reranker, fake = make_reranker([0.9, 0.8])
    candidates = [candidate(1), candidate(2)]

    assert reranker.rerank("query", candidates, top_n=0) == []
    assert reranker.rerank("query", candidates, top_n=-3) == []
    assert fake.calls == []


def test_model_name_exposes_the_configured_id() -> None:
    assert CrossEncoderReranker("cross-encoder/example").model_name == "cross-encoder/example"


def test_from_settings_reads_the_model_name_from_settings(monkeypatch) -> None:
    """The model id comes from configuration, never from a request."""
    import app.generation.rag.retrieval.reranker as reranker_module

    monkeypatch.setattr(
        reranker_module,
        "get_settings",
        lambda: SimpleNamespace(RERANKER_MODEL="cross-encoder/from-settings"),
    )

    assert CrossEncoderReranker.from_settings().model_name == "cross-encoder/from-settings"


# --- the lazy loader, exercised for real (no torch) ------------------------
#
# ``install()`` replaces ``_ensure_loaded``, which is why its body had no
# coverage: the double-checked load, the model name handed to the constructor,
# and the load/score timings were all unobserved. These tests supply a fake
# ``sentence_transformers`` module through ``sys.modules``, so the import inside
# ``_ensure_loaded`` resolves to it and no torch is imported anywhere.


def fake_sentence_transformers(monkeypatch, model_factory):
    """Install a stub ``sentence_transformers`` whose ``CrossEncoder`` is ours."""
    module = SimpleNamespace(CrossEncoder=model_factory)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return module


def test_the_loader_builds_the_model_from_the_configured_name(monkeypatch) -> None:
    requested: list[str] = []
    model = FakeCrossEncoder([0.5])

    def factory(name: str) -> FakeCrossEncoder:
        requested.append(name)
        return model

    fake_sentence_transformers(monkeypatch, factory)
    reranker = CrossEncoderReranker("cross-encoder/configured-id")

    assert reranker._ensure_loaded() is model                             # noqa: SLF001
    assert requested == ["cross-encoder/configured-id"]


def test_the_loader_caches_the_model_in_the_instance_slot(monkeypatch) -> None:
    """``self._model`` holds the model, so the second call takes the fast path."""
    model = FakeCrossEncoder([0.5])
    built = 0

    def factory(name: str) -> FakeCrossEncoder:
        nonlocal built
        built += 1
        return model

    fake_sentence_transformers(monkeypatch, factory)
    reranker = CrossEncoderReranker("model")

    reranker._ensure_loaded()                                             # noqa: SLF001
    reranker._ensure_loaded()                                             # noqa: SLF001

    assert reranker._model is model                                       # noqa: SLF001
    assert built == 1, "the model was loaded more than once"


def test_a_load_failure_names_the_model_and_the_cause(monkeypatch) -> None:
    """The raised text is the only diagnostic an operator gets from a 502."""

    class BrokenCrossEncoder:
        def __init__(self, *args, **kwargs) -> None:
            raise OSError("weights not found")

    fake_sentence_transformers(monkeypatch, BrokenCrossEncoder)

    with pytest.raises(RerankerUnavailable) as raised:
        CrossEncoderReranker("missing/model").load()

    message = str(raised.value)
    assert "missing/model" in message
    assert "OSError" in message
    assert "weights not found" in message


def test_a_scoring_failure_names_the_model_the_pair_count_and_the_cause() -> None:
    reranker, _ = make_reranker(error=RuntimeError("CUDA out of memory"))

    with pytest.raises(RerankerUnavailable) as raised:
        reranker.score("query", ["a", "b", "c"])

    message = str(raised.value)
    assert "fake-cross-encoder" in message
    assert "3 pairs" in message
    assert "RuntimeError" in message
    assert "CUDA out of memory" in message


def test_the_loader_reports_its_own_timing(monkeypatch) -> None:
    """``load_ms`` is ``(end - start) * 1000``: a divisor or a sum is invisible live."""
    import app.generation.rag.retrieval.reranker as reranker_module

    class FakeClock:
        def __init__(self, ticks: list[float]) -> None:
            self.ticks = list(ticks)

        def perf_counter(self) -> float:
            return self.ticks.pop(0)

    log = RecordingLogger()
    model = FakeCrossEncoder([0.5])
    fake_sentence_transformers(monkeypatch, lambda name: model)
    monkeypatch.setattr(reranker_module, "time", FakeClock([10.0, 11.0]))
    monkeypatch.setattr(reranker_module, "log", log)

    CrossEncoderReranker("timed-model").load()

    # A whole-second delta: int() truncation cannot hide a *1001 or /1000 slip.
    assert log.infos == [("reranker_loaded", {"model": "timed-model", "load_ms": 1000})]


def test_scoring_reports_its_own_timing_and_pair_count(monkeypatch) -> None:
    import app.generation.rag.retrieval.reranker as reranker_module

    class FakeClock:
        def __init__(self, ticks: list[float]) -> None:
            self.ticks = list(ticks)

        def perf_counter(self) -> float:
            return self.ticks.pop(0)

    log = RecordingLogger()
    reranker, _ = make_reranker([0.1, 0.2])
    # One tick pair after the explicit ``started`` in score(). A whole-second
    # delta so ``int()`` truncation cannot mask a scaling slip.
    monkeypatch.setattr(reranker_module, "time", FakeClock([1.0, 2.0]))
    monkeypatch.setattr(reranker_module, "log", log)

    reranker.score("query", ["a", "b"])

    assert log.infos == [("reranker_scored", {"pairs": 2, "score_ms": 1000})]
