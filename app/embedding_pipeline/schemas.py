"""Pydantic v2 models for the embedding pipeline.

Two layers:

* Input — mirrors the normalized historical-budget JSON (a budget with a list of
  components). Request models forbid unknown fields and enforce size caps: the
  endpoint is unauthenticated and every chunk costs money to embed.
* Output — a chunk ready to embed, the same chunk with its vector attached, and
  the aggregate stats returned alongside.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Closed universes. A typo or an unexpected value fails validation loudly
# instead of silently leaking into the metadata that future retrieval filters on.
Sector = Literal["finance", "ecommerce", "healthcare", "industrial"]
Complexity = Literal["low", "medium", "high"]

# Request caps. Sized to accept the sample corpus (17 budgets, 60 components)
# with wide headroom while bounding memory, response size and embedding spend.
# A chunk is 1536 floats, so 500 chunks is already ~30 MB of JSON in a response.
MAX_BUDGETS_PER_REQUEST = 100
MAX_COMPONENTS_PER_BUDGET = 100
MAX_CHUNKS_PER_REQUEST = 500
MAX_NAME_CHARS = 500
MAX_DESCRIPTION_CHARS = 20_000
MAX_TECH_STACK_ENTRIES = 50
MAX_HOURS_PER_COMPONENT = 100_000
# Session 8 — the persistent ingest takes ONE document per request, so the batch
# caps above now belong to the chunking comparison (Track B). These bound the
# two fields that are stored rather than validated away.
MAX_SOURCE_PATH_CHARS = 512
MAX_DOCUMENT_TYPE_CHARS = 50
MAX_QUERY_CHARS = 2_000
MAX_SEARCH_K = 50


class _StrictModel(BaseModel):
    """Base for request models: unknown fields are a client bug, not a detail."""

    model_config = ConfigDict(extra="forbid")


class ClientMetadata(_StrictModel):
    """Who the budget belongs to. Travels as filterable context, not embedded."""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    sector: Sector
    country: str = Field(min_length=1, max_length=100)


class BudgetComponent(_StrictModel):
    """A single line item of a historical budget."""

    component_id: str = Field(
        min_length=1,
        max_length=MAX_NAME_CHARS,
        description="Stable id within the budget, e.g. 'AUTH-001'.",
    )
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    tech_stack: list[str] = Field(
        default_factory=list,
        max_length=MAX_TECH_STACK_ENTRIES,
        description="Technologies involved in this component.",
    )
    estimated_hours: int = Field(ge=0, le=MAX_HOURS_PER_COMPONENT)
    complexity: Complexity
    dependencies: list[str] = Field(
        default_factory=list,
        max_length=MAX_TECH_STACK_ENTRIES,
        description="component_ids this one depends on.",
    )


class Budget(_StrictModel):
    """A complete historical budget with its components."""

    budget_id: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    client_metadata: ClientMetadata
    project_summary: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    main_technology: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    year: int = Field(ge=2000, le=2100)
    total_estimated_hours: int = Field(ge=0)
    components: list[BudgetComponent] = Field(
        min_length=1, max_length=MAX_COMPONENTS_PER_BUDGET
    )

    @model_validator(mode="after")
    def _component_ids_are_unique(self) -> "Budget":
        ids = [component.component_id for component in self.components]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "component_id values must be unique within a budget: "
                "chunk_id is '{budget_id}::{component_id}' and must be traceable"
            )
        return self


class Chunk(BaseModel):
    """A fragment ready to be embedded.

    ``text`` is what gets sent to the embeddings API; ``metadata`` carries
    filterable fields that travel alongside the chunk but are NOT embedded.
    """

    chunk_id: str
    text: str
    metadata: dict = Field(default_factory=dict)
    token_count: int = Field(ge=0)


class EmbeddedChunk(Chunk):
    """A :class:`Chunk` with its embedding vector attached."""

    embedding: list[float]


class IngestRequest(_StrictModel):
    """Payload for ``POST /embeddings/ingest`` (Session 8: one document).

    One request = one document. ``content`` is the full budget JSON, validated
    against :class:`Budget` so a malformed corpus fails with a 422 before
    touching the database or the embeddings API.

    ``source_path`` is provenance and the uniqueness key, **not** a file that is
    read: path traversal is not the risk here, unbounded storage is. Hence the
    length cap and the control-character rejection, not a path allow-list.
    """

    source_path: str = Field(
        min_length=1,
        max_length=MAX_SOURCE_PATH_CHARS,
        description="Provenance of the document, unique per ingest.",
    )
    document_type: str = Field(
        min_length=1,
        max_length=MAX_DOCUMENT_TYPE_CHARS,
        description="Document family, e.g. 'historical_budget'.",
    )
    content: Budget = Field(description="Full budget JSON, as produced upstream.")

    @field_validator("source_path")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source_path must not contain control characters")
        if value.strip() != value:
            raise ValueError("source_path must not have leading or trailing whitespace")
        return value


class IngestResponse(BaseModel):
    """Response for ``POST /embeddings/ingest``: identifiers + ingest metrics.

    Vectors no longer travel over HTTP — they are persisted in pgvector.
    """

    document_id: int = Field(description="Primary key of the persisted document.")
    chunks_created: int = Field(ge=0, description="Chunks persisted for this document.")
    embedding_dimension: int = Field(description="Dimensionality of the stored vectors.")
    ingestion_time_ms: int = Field(ge=0, description="Wall-clock ingest time.")


class SearchRequest(_StrictModel):
    """Payload for ``POST /search``."""

    query: str = Field(
        min_length=1, max_length=MAX_QUERY_CHARS, description="Free-text semantic query."
    )
    k: int = Field(default=5, ge=1, le=MAX_SEARCH_K, description="Nearest chunks to return.")


class SearchHit(BaseModel):
    """One ranked chunk.

    ``chunk_id`` is the database primary key; the traceable corpus id
    (``BUD-...::COMP-...``) travels inside ``metadata``.
    """

    chunk_id: int
    document_id: int
    chunk_type: str
    content: str
    distance: float = Field(description="Cosine distance (lower = more similar).")
    metadata: dict


class SearchResponse(BaseModel):
    """Response for ``POST /search``."""

    query: str
    k: int
    search_time_ms: int = Field(ge=0)
    results: list[SearchHit]
