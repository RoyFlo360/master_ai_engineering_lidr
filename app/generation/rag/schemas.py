"""Contracts for the grounded-estimation chain (Session 9 live layer, T0B).

The retrieval half lives in ``retrieval/schemas.py`` (:class:`RetrievedChunk` and
friends). This module holds the *generation* half:

    transcript ─► EstimationQuery ─► retrieved chunks ─► GroundedEstimate

The two meet at ``RetrievedChunk``: the assembler consumes exactly what
``retrieval.pipeline.retrieve`` produces, so the chain never re-implements
retrieval.

Design notes
------------
* ``EstimationQuery`` is the reformulator's structured output. It carries both a
  human-readable ``summary`` and a canonical ``search_query`` — the string that
  is actually embedded and run through the lexical branch. They are separate on
  purpose: a transcript is not a query, and the summary is for the answer while
  the search string is for the index.
* ``GroundedEstimate.citations`` is not decoration. ``validation.py`` checks that
  every cited ``chunk_id`` was actually in the retrieved set, and refuses an
  estimate that cites something it was never shown.
* ``grounded`` is the honesty flag. When nothing was retrieved (or everything was
  filtered out), the estimate is returned with ``grounded=False`` and
  ``citations=[]`` rather than silently answered from the model's memory.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Bounds. Kept as module constants so tests and the router agree.
MAX_TRANSCRIPT_CHARS = 50_000
MIN_TRANSCRIPT_CHARS = 100
MAX_SEARCH_QUERY_CHARS = 2_000
MAX_SUMMARY_CHARS = 2_000
MAX_MODULES = 20
MAX_TASKS_PER_MODULE = 30
MAX_NAME_CHARS = 200
MAX_CITATION_CLAIM_CHARS = 500
MAX_IDEMPOTENCY_KEY_CHARS = 128


class EstimationQuery(BaseModel):
    """What the transcript actually asks for, distilled.

    Produced by ``query_reformulator``. Everything except ``summary`` and
    ``search_query`` is optional: a rambling transcript may not state a sector or
    a technology, and inventing one would poison the retrieval filters.
    """

    summary: str = Field(
        min_length=1,
        max_length=MAX_SUMMARY_CHARS,
        description="Two or three sentences describing the project to estimate.",
    )
    search_query: str = Field(
        min_length=1,
        max_length=MAX_SEARCH_QUERY_CHARS,
        description="Canonical search string embedded and run through the lexical "
        "branch. Domain terms preserved, no invented requirements.",
    )
    sector: str | None = Field(
        default=None,
        max_length=MAX_NAME_CHARS,
        description="Client sector when the transcript states one; used as a filter, "
        "so a guess is worse than None.",
    )
    technologies: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Technologies explicitly named in the transcript.",
    )
    project_year: int | None = Field(
        default=None, ge=2000, le=2100, description="Year when the transcript states one."
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="How complete the brief is. Low confidence must not silently "
        "become a confident estimate downstream.",
    )
    source: Literal["llm", "fallback"] = Field(
        default="llm",
        description="'fallback' when the LLM was unavailable and the query was "
        "built by truncating the transcript. Downstream confidence is capped.",
    )


class Citation(BaseModel):
    """One claim in the estimate, tied to the chunk that supports it."""

    chunk_id: int = Field(description="RetrievedChunk.chunk_id this claim rests on.")
    budget_id: str | None = Field(
        default=None, description="Parent budget, copied from the chunk metadata."
    )
    claim: str = Field(
        min_length=1,
        max_length=MAX_CITATION_CLAIM_CHARS,
        description="What the estimate asserts that this chunk supports.",
    )


class EstimatedTask(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    estimated_hours: int = Field(ge=0, le=10_000)


class EstimatedModule(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    tasks: list[EstimatedTask] = Field(min_length=1, max_length=MAX_TASKS_PER_MODULE)
    estimated_hours: int = Field(ge=0, le=100_000)


class GroundedEstimate(BaseModel):
    """The estimate, with its evidence attached."""

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    modules: list[EstimatedModule] = Field(default_factory=list, max_length=MAX_MODULES)
    total_hours: int = Field(ge=0, le=1_000_000)
    citations: list[Citation] = Field(default_factory=list)
    confidence_pct: int = Field(ge=0, le=100)
    grounded: bool = Field(
        description="False when no retrieved chunk reached the prompt. A caller "
        "must not present an ungrounded estimate as evidence-backed."
    )


class EstimateFromTranscriptRequest(BaseModel):
    """Payload for ``POST /v1/estimate/from-transcript``."""

    model_config = {"extra": "forbid"}

    transcript: str = Field(
        min_length=MIN_TRANSCRIPT_CHARS, max_length=MAX_TRANSCRIPT_CHARS
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=MAX_IDEMPOTENCY_KEY_CHARS,
        description="Replays the same estimate for the same key instead of paying "
        "for a second generation.",
    )
    top_k: int = Field(default=5, ge=1, le=50)
    rerank: bool | None = Field(
        default=None, description="None defers to Settings.RERANKER_ENABLED."
    )
    search_mode: Literal["vector", "hybrid"] | None = Field(
        default=None, description="None defers to Settings.RETRIEVAL_SEARCH_MODE."
    )


class EstimateFromTranscriptResponse(BaseModel):
    """What the chain returns. Every field is observable on purpose."""

    estimate: GroundedEstimate
    query: EstimationQuery
    chunks: list  # list[RetrievedChunk]; loose to avoid a circular import
    retrieved: int = Field(ge=0)
    cached: bool = Field(description="True when replayed from the idempotency store.")
    validation_retries: int = Field(ge=0, le=1)
    pipeline_ms: int = Field(ge=0)


class StageEnvelope(BaseModel):
    """Common shape for the four stateless stage endpoints."""

    stage: Literal["reformulate", "retrieve", "assemble", "generate"]
    latency_ms: int = Field(ge=0)
