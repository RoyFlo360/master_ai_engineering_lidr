"""Stage timing for the estimation chain.

One helper, used by every stage, so the logs answer "where did the 4 seconds go"
without a profiling session. It is deliberately tiny: a context manager that
records a duration and emits one structured event, and never swallows an
exception (a stage that raises still gets its timing logged, with the outcome).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

import structlog

log = structlog.get_logger(__name__)


@contextmanager
def log_stage(stage: str, request_id: str | None = None, **fields: object) -> Iterator[dict]:
    """Time one stage and log ``stage_done`` (or ``stage_failed``) with its duration.

    ``fields`` are extra structured attributes (chunk counts, model names). The
    yielded dict can be mutated by the caller to add things known only at the end
    (for example the number of citations). Exceptions propagate untouched — this
    is observability, not error handling.
    """
    started = time.perf_counter()
    record: dict = dict(fields)
    try:
        yield record
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        log.error(
            "stage_failed",
            stage=stage,
            request_id=request_id,
            latency_ms=elapsed,
            error_type=type(exc).__name__,
            **record,
        )
        raise
    elapsed = int((time.perf_counter() - started) * 1000)
    log.info("stage_done", stage=stage, request_id=request_id, latency_ms=elapsed, **record)
