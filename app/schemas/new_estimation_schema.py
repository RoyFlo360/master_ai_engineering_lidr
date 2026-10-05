from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.schemas.estimation_schema import TokenUsage


class ProjectType(str, Enum):
    MOBILE_APP = "mobile_app"
    WEB_SAAS = "web_saas"
    INTERNAL_TOOL = "internal_tool"
    DATA_PIPELINE = "data_pipeline"

class DetailLevel(str, Enum):
    SUMMARY = "summary"
    MEDIUM = "medium"
    DETAILED = "detailed"

class OutputFormat(str, Enum):
    PHASES_TABLE = "phases_table"
    LINE_ITEMS = "line_items"
    NARRATIVE = "narrative"

class EstimationRequest(BaseModel):
    """Typed payload sent by the business backend or Streamlit form."""
    description: str = Field(
        min_length=20,
        max_length=80000,
        description="Free-text description or transcription of the project to estimate.",
    )
    project_type: ProjectType = Field(description="Coarse-grained project category.")
    detail_level: DetailLevel = Field(description="How deep the estimation should go.")
    output_format: OutputFormat = Field(description="Shape of the rendered estimation.")


# --- Structured response ----------------------------------------------------


OUT_OF_SCOPE_PREFIX = "Out of scope:"
LOW_CONFIDENCE_THRESHOLD = 30


class Phase(BaseModel):
    """One phase in the breakdown of an estimation."""

    name: str = Field(min_length=1, max_length=64)
    duration_weeks: int = Field(ge=1, le=52)
    cost_eur: int = Field(ge=0, le=1_000_000)
    summary: str = Field(min_length=10, max_length=600)


class EstimationResult(BaseModel):
    """Structured estimation. The two validators below are the business rules
    that the LLM cannot break — Instructor will re-prompt the model when one
    of them raises.

    Field order is deliberate: ``phases`` comes BEFORE the totals so the LLM
    commits to the per-phase numbers first (autoregressive generation) and
    then only needs to sum them when filling the totals. Putting totals first
    leads the model to pick a round number and then back-fit phases to it,
    which it does very badly arithmetically — particularly with smaller
    models like ``gpt-4o-mini``.
    """

    summary: str = Field(min_length=10, max_length=1200)
    confidence_pct: int = Field(ge=0, le=100)
    phases: list[Phase] = Field(min_length=1, max_length=8)
    total_duration_weeks: int = Field(ge=1, le=104)
    total_cost_eur: int = Field(ge=0, le=2_000_000)

    @model_validator(mode="after")
    def phases_sum_matches_total(self) -> EstimationResult:
        phase_sum = sum(p.cost_eur for p in self.phases)
        if phase_sum != self.total_cost_eur:
            raise ValueError(
                f"phases sum ({phase_sum} EUR) does not match total_cost_eur "
                f"({self.total_cost_eur} EUR); adjust either the phases or the total"
            )
        return self

    @model_validator(mode="after")
    def low_confidence_requires_out_of_scope_prefix(self) -> EstimationResult:
        if self.confidence_pct < LOW_CONFIDENCE_THRESHOLD and not self.summary.startswith(
            OUT_OF_SCOPE_PREFIX
        ):
            raise ValueError(
                f"confidence_pct < {LOW_CONFIDENCE_THRESHOLD} requires summary to "
                f"start with {OUT_OF_SCOPE_PREFIX!r}; refuse the estimation if the "
                f"description is too vague to size"
            )
        return self


class EstimationResponse(BaseModel):
    """Estimation rendered as free text, plus how the call was served.

    This is the response of the typed ``POST /api/v1/new-estimate`` flow, which
    asks the model for Markdown text and reports the call's billing. The
    structured flows (conversational sessions, the Session-4 service pipeline)
    validate the model output against :class:`EstimationResult` instead and
    answer with :class:`StructuredEstimationResponse` — keeping the two shapes
    apart is deliberate, because a free-text answer has no phases to validate
    and a structured answer has no rendered ``text``.

    ``usage`` carries the tokens and cost of the call behind ``text``. On a cache
    hit the token counts are the cached call's — an identical prompt tokenises
    identically, so they do not change between replays — and the cost is zero,
    because the replay was not billed.
    """

    text: str = Field(description="Estimation rendered by the LLM as free text.")
    prompt_version: str = Field(description="Identifier of the prompt template used.")
    cache_hit: bool = Field(description="True when the text was replayed from the cache.")
    usage: TokenUsage = Field(description="Token counts and cost of this call.")


class StructuredEstimationResponse(BaseModel):
    """Validated estimation as typed phases, plus how the call was served.

    Returned by the conversational session flow and by
    :meth:`EstimationService.estimate`. ``result`` has already passed the
    ``EstimationResult`` validators (and, where wired, the output guardrail), so
    callers can render phases directly with no further parsing.

    There is no ``usage`` field: the conversational path is not cached and does
    not thread the provider usage object through Instructor, so a token count
    here would be invented. ``cache_hit`` is therefore always ``False`` on the
    conversational path — every turn depends on the history and metadata, so a
    replay is never the same call.
    """

    result: EstimationResult = Field(description="Validated structured estimation.")
    prompt_version: str = Field(description="Identifier of the prompt template used.")
    cache_hit: bool = Field(description="True when the result was replayed from the cache.")