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

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Closed universes. A typo or an unexpected value fails validation loudly
# instead of silently leaking into the metadata that future retrieval filters on.
Sector = Literal["finance", "ecommerce", "healthcare", "industrial"]
Complexity = Literal["low", "medium", "high"]

# Request caps. Sized to accept the 15-budget sample (52 components) with wide
# headroom while bounding memory, response size and embedding spend. A chunk is
# 1536 floats, so 500 chunks is already ~30 MB of JSON in the response.
MAX_BUDGETS_PER_REQUEST = 100
MAX_COMPONENTS_PER_BUDGET = 100
MAX_CHUNKS_PER_REQUEST = 500
MAX_NAME_CHARS = 500
MAX_DESCRIPTION_CHARS = 20_000
MAX_TECH_STACK_ENTRIES = 50
MAX_HOURS_PER_COMPONENT = 100_000


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


class IngestStats(BaseModel):
    """Aggregate counters returned with an ingest response."""

    total_budgets: int = Field(ge=0)
    total_chunks: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0.0)


class IngestRequest(_StrictModel):
    """Payload for ``POST /embeddings/ingest``."""

    budgets: list[Budget] = Field(min_length=1, max_length=MAX_BUDGETS_PER_REQUEST)

    @model_validator(mode="after")
    def _identifiers_unique_and_bounded(self) -> "IngestRequest":
        budget_ids = [budget.budget_id for budget in self.budgets]
        if len(set(budget_ids)) != len(budget_ids):
            raise ValueError(
                "budget_id values must be unique within a request: "
                "chunk_id is derived from the budget id"
            )
        total_components = sum(len(budget.components) for budget in self.budgets)
        if total_components > MAX_CHUNKS_PER_REQUEST:
            raise ValueError(
                f"request would produce {total_components} chunks; "
                f"the cap is {MAX_CHUNKS_PER_REQUEST}"
            )
        return self


class IngestResponse(BaseModel):
    """Response for ``POST /embeddings/ingest``."""

    chunks: list[EmbeddedChunk]
    stats: IngestStats
