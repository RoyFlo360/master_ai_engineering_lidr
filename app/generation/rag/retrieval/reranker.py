"""Cross-encoder reranker for the Session-10 retrieval pipeline.

**Bi-encoder vs cross-encoder.** The vector branch of retrieval is a
*bi-encoder*: the corpus chunks were embedded offline, once, and a query is
embedded at request time; relevance is the cosine distance between two vectors
that never saw each other. That is what makes k-NN over millions of chunks
affordable, and it is also its ceiling — the two encodings cannot condition on
each other, so fine-grained relevance ("does this chunk answer *this* query?")
is approximated by geometric proximity.

A *cross-encoder* feeds the query and the document through one transformer at
the same time, as a single pair, and emits a relevance score. Attention can
compare query terms against document terms directly, which ranks far better —
and costs one full forward pass per candidate, so it cannot be run over a whole
corpus.

**Recall, then rerank.** That is the shape of the pipeline: a cheap, high-recall
first stage (vector k-NN, optionally fused with lexical full-text search by RRF)
fetches ``RETRIEVAL_RECALL_TOP_K`` candidates; the cross-encoder then scores
only those pairs and keeps the best ``RERANK_TOP_N``. The expensive model sees
tens of documents, not thousands, and the caller gets the first stage's recall
with the second stage's precision.

**Import discipline.** ``sentence_transformers`` and ``torch`` are imported
inside ``_ensure_loaded()``, never at module import: importing this module must
stay cheap (the API imports it on every start) and must not pull a multi-hundred
megabyte runtime into a process that never enables reranking. The model is
loaded lazily, once, under a lock.

**Failure policy.** Loading and scoring are the only things that can fail here.
Both raise ``RerankerUnavailable`` (defined in
``app.generation.rag.retrieval.errors``); the pipeline maps it to a 502 rather
than silently returning recall order, which would report a configuration that
never ran. The import is local to the raising method so this module stays
importable even when the model runtime — or that error module — is absent.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence, TypeVar

import structlog

from app.config import get_settings

try:  # Re-exported for callers that expect one import site for the reranker API.
    from app.generation.rag.retrieval.errors import RerankerUnavailable
except ImportError:  # pragma: no cover - only while that module is landing
    RerankerUnavailable = None  # type: ignore[assignment]

log = structlog.get_logger(__name__)

T = TypeVar("T")

__all__ = ["CrossEncoderReranker", "RerankerUnavailable"]


class CrossEncoderReranker:
    """Lazy wrapper around a ``sentence_transformers.CrossEncoder``.

    One instance per process is enough: ``_ensure_loaded`` makes the first call
    pay for the download/load and every later call reuse the same model. The
    wrapper is deliberately not a module-level singleton, so tests can construct
    one with a fake model name (or patch ``_model``) without touching globals.
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        # ``None`` until the first load; the sentinel also keeps ``load()`` from
        # re-downloading when the model is already resident.
        self._model: Any | None = None
        # Guards the load only; scoring runs outside the lock (``predict`` is
        # thread-safe for reads and must not serialise concurrent requests).
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls) -> "CrossEncoderReranker":
        """Build the reranker from ``Settings.RERANKER_MODEL``.

        The model name comes from configuration, never from a request: a caller
        must not be able to make the process download arbitrary weights.
        """
        return cls(get_settings().RERANKER_MODEL)

    @property
    def model_name(self) -> str:
        """The Hugging Face id this instance will load."""
        return self._model_name

    def load(self) -> None:
        """Force the load now instead of on the first ``score`` call.

        Used by the pre-flight gate (``verify_reranker``) so a broken
        environment fails before a request does.
        """
        self._ensure_loaded()

    def _ensure_loaded(self) -> Any:
        """Return the model, loading it once under a lock.

        Double-checked: the fast path is a plain attribute read, so steady-state
        calls never contend. Failures are wrapped in ``RerankerUnavailable``
        with the original exception chained (the type is preserved in the chain
        and in the pipeline's log line).
        """
        if self._model is not None:
            return self._model

        with self._lock:
            if self._model is not None:
                return self._model

            started = time.perf_counter()
            try:
                # Imported here, not at module scope: see the module docstring.
                from sentence_transformers import CrossEncoder

                model = CrossEncoder(self._model_name)
            except Exception as exc:  # noqa: BLE001 - any load failure is the same failure
                from app.generation.rag.retrieval.errors import RerankerUnavailable

                raise RerankerUnavailable(
                    f"Could not load cross-encoder model {self._model_name!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            self._model = model
            load_ms = int((time.perf_counter() - started) * 1000)
            log.info("reranker_loaded", model=self._model_name, load_ms=load_ms)
            return model

    def score(self, query: str, documents: list[str]) -> list[float]:
        """Score ``documents`` against ``query``; one batched ``predict`` call.

        Returns one float per document, in input order. Higher is more relevant
        (the cross-encoder's score is an unbounded logit, not a probability, so
        only the ordering is meaningful).
        """
        if not documents:
            return []

        model = self._ensure_loaded()
        pairs = [(query, document) for document in documents]

        started = time.perf_counter()
        try:
            raw_scores = model.predict(pairs)
        except Exception as exc:  # noqa: BLE001 - any scoring failure is the same failure
            from app.generation.rag.retrieval.errors import RerankerUnavailable

            raise RerankerUnavailable(
                f"Cross-encoder {self._model_name!r} failed to score "
                f"{len(pairs)} pairs: {type(exc).__name__}: {exc}"
            ) from exc

        score_ms = int((time.perf_counter() - started) * 1000)
        log.info("reranker_scored", pairs=len(pairs), score_ms=score_ms)
        return [float(value) for value in raw_scores]

    def rerank(
        self,
        query: str,
        candidates: Sequence[T],
        *,
        top_n: int,
        text_of: Callable[[T], str] = lambda candidate: candidate.content,
    ) -> list[T]:
        """Re-order ``candidates`` by cross-encoder relevance and keep ``top_n``.

        ``text_of`` is the only coupling to the candidate type: the pipeline
        passes ``RetrievedChunk``s and the default reads ``.content``; a test
        can pass plain objects with a custom accessor.

        The sort is stable, so equal scores keep their incoming order — the
        first stage's ranking, which is the only tie-break signal available and
        a deterministic one. ``top_n <= 0`` yields an empty list rather than a
        Python negative-slice surprise.
        """
        if not candidates or top_n <= 0:
            return []

        documents = [text_of(candidate) for candidate in candidates]
        scores = self.score(query, documents)

        # Sort by score descending, indices ascending on ties (Python's sort is
        # stable and the key alone does not compare the elements).
        order = sorted(range(len(candidates)), key=lambda position: -scores[position])
        return [candidates[position] for position in order[:top_n]]
