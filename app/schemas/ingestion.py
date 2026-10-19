"""Pydantic v2 schemas for the ingestion HTTP layer.

Two endpoints, three shapes. ``extra="forbid"`` everywhere: a client that sends
``{"source": "x"}`` instead of ``{"source_name": "x"}`` gets a 422, not a
silently ignored field.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Mirrors ``app.persistence.models.JOB_STATUSES`` without importing SQLAlchemy.
JobStatus = Literal["pending", "running", "completed", "failed"]

#: Hard cap on the source name. Anything longer is a malformed request (422),
#: not a lookup miss — the bound is also what keeps a hostile 1 MB body from
#: reaching the catalog.
MAX_SOURCE_NAME_LENGTH = 128


class IngestionRunRequest(BaseModel):
    """Body of ``POST /api/v1/ingestion/runs``."""

    model_config = ConfigDict(extra="forbid")

    source_name: str = Field(
        min_length=1,
        max_length=MAX_SOURCE_NAME_LENGTH,
        description=(
            "Name of an ``include`` source in the catalog. Anything else "
            "(unknown, review, exclude) is rejected with 404/400."
        ),
    )


class IngestionRunResponse(BaseModel):
    """Response of ``POST /api/v1/ingestion/runs``. Returned with HTTP 202."""

    job_id: uuid.UUID
    source_name: str
    status: JobStatus


class IngestionJobView(BaseModel):
    """Response of ``GET /api/v1/ingestion/jobs/{job_id}``."""

    job_id: uuid.UUID
    source_name: str
    status: JobStatus
    documents_count: int
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


__all__ = [
    "IngestionJobView",
    "IngestionRunRequest",
    "IngestionRunResponse",
    "JobStatus",
    "MAX_SOURCE_NAME_LENGTH",
]
