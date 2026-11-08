"""In-process idempotency for ``POST /v1/estimate/from-transcript``.

Why in-process and not Redis: the chain is a single-process demo surface, and the
only thing the store must guarantee today is that a client retrying with the same
``idempotency_key`` does not pay for a second reformulation and a second
generation. A Redis-backed store is the production answer (it survives restarts
and is shared across workers); building it here would add a connection, a TTL
policy and a failure mode to an exercise that needs none of them.

Consequences, stated so nobody is surprised:

* **Nothing survives a restart.** A replay after a deploy regenerates the
  estimate.
* **Per worker.** With ``uvicorn --workers N`` each worker has its own store, so
  a replay may miss by landing on a different worker.
* **Bounded.** 256 entries, oldest-first (FIFO) eviction, so a long-running
  process cannot leak memory by serving a distinct key per request.

Key format: ``sha256(idempotency_key + "\\x00" + transcript)``. Hashing the
transcript *with* the key is deliberate: the same key sent with a different
transcript is a different request and must not replay the earlier estimate.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

from app.generation.rag.schemas import EstimateFromTranscriptResponse

#: Entries kept before the oldest is evicted.
DEFAULT_CAPACITY = 256


def make_key(idempotency_key: str, transcript: str) -> str:
    """Derive the store key from the client key and the transcript it applied to.

    The NUL separator stops ``("ab", "c")`` and ``("a", "bc")`` from sharing a
    key, and hashing keeps the entry small regardless of transcript length while
    leaving no raw transcript text in memory beyond the response itself.
    """
    digest = hashlib.sha256()
    digest.update(idempotency_key.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(transcript.encode("utf-8"))
    return digest.hexdigest()


class IdempotencyStore:
    """Thread-safe, bounded, process-local map of key -> completed response.

    The lock matters because the FastAPI route runs on the event loop while a
    generation call blocks a worker thread; the store is touched from both
    sides of an ``await``.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._entries: OrderedDict[str, EstimateFromTranscriptResponse] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> EstimateFromTranscriptResponse | None:
        """Return the stored response for ``key``, or ``None``."""
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, value: EstimateFromTranscriptResponse) -> None:
        """Store ``value`` under ``key`` and evict the oldest entry if over capacity.

        Re-putting an existing key keeps its original insertion position: eviction
        is FIFO by first insert, not LRU, so a hot key cannot starve a cold one.
        """
        with self._lock:
            self._entries[key] = value
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry. Used by tests; never called by the chain."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_store = IdempotencyStore()


def get_idempotency_store() -> IdempotencyStore:
    """The process-wide store.

    A module-level singleton rather than a ``Depends`` factory because the
    orchestrator is not an HTTP component and tests call it directly; a test that
    wants a clean slate calls ``get_idempotency_store().clear()``.
    """
    return _store
