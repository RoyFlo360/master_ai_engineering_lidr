"""In-process running totals for what the estimator has spent.

The ledger lives in the API process's memory, which is the point: the sidebar
reads the total from the API instead of counting locally, so it survives a
browser refresh or a new Streamlit session.

It does *not* survive the API process. A restart starts the total over from
zero, and in development so does every uvicorn ``--reload`` after a code change,
because that replaces the worker process. Redis is the upgrade path when the
total has to outlive the server; :meth:`UsageTotals.as_dict` is the shape it
would have to store.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass
class UsageTotals:
    """Running totals over every call this process has served."""

    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            # Costs arrive rounded to six decimals; summing them can leave float
            # noise, so the total is rounded on the way out as well.
            "cost_usd": round(self.cost_usd, 6),
        }


class UsageLedger:
    """Accumulates one call's usage at a time, under a lock.

    Requests run on a threadpool, so two can interleave: the lock keeps the
    totals consistent instead of relying on ``+=`` being atomic.
    """

    def __init__(self) -> None:
        self._totals = UsageTotals()
        self._lock = Lock()

    def record(self, usage: dict[str, Any], *, cache_hit: bool) -> None:
        """Add one finished call.

        A cache hit still counts as a call, and as a hit, while contributing
        zero cost — nothing was billed for the replay.
        """
        with self._lock:
            self._totals.calls += 1
            self._totals.cache_hits += 1 if cache_hit else 0
            self._totals.input_tokens += usage.get("input_tokens", 0)
            self._totals.output_tokens += usage.get("output_tokens", 0)
            self._totals.total_tokens += usage.get("total_tokens", 0)
            self._totals.cost_usd += usage.get("cost_usd", 0.0)

    def totals(self) -> dict[str, Any]:
        """A consistent snapshot of the running totals."""
        with self._lock:
            return self._totals.as_dict()
