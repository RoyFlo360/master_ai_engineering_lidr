"""Exceptions raised by the query-time retrieval layer (Session 10).

Both are transport-agnostic: the pipeline raises them, the router maps them to
HTTP status codes. Nothing below this layer imports FastAPI.

``RerankerUnavailable`` lives here — not in ``reranker.py`` — because the
pipeline must be able to raise it without importing the module that pulls
``sentence-transformers``. ``reranker.py`` re-exports this class.
"""

from __future__ import annotations


class RetrievalUnavailable(RuntimeError):
    """The vector store or the embedder cannot run this request.

    Caused by a deployment without an OpenAI key, a non-Postgres database, or a
    mismatched embedding model — the same three conditions the pre-Session-10
    ``/search`` route mapped to a generic 500.
    """


class RerankerUnavailable(RuntimeError):
    """The cross-encoder could not load or score the candidates.

    Fail-closed on purpose: a caller that asked for ``rerank=true`` must never
    receive recall order dressed up as reranked output.
    """
