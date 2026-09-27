from enum import Enum
from pydantic import BaseModel, Field

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
    description: str = Field(min_length=20, max_length=2000)
    project_type: ProjectType = Field(description="Coarse-grained project category.")
    detail_level: DetailLevel = Field(description="How deep the estimation should go.")
    output_format: OutputFormat = Field(description="Shape of the rendered estimation.")

class EstimationResponse(BaseModel):
    """Estimation rendered as free text, plus how the call was served.

    ``usage`` carries the tokens and cost of the call behind ``text``. On a cache
    hit the token counts are the cached call's — an identical prompt tokenises
    identically, so they do not change between replays — and the cost is zero,
    because the replay was not billed.
    """

    text: str = Field(description="Estimation rendered by the LLM as free text.")
    prompt_version: str = Field(description="Identifier of the prompt template used.")
    cache_hit: bool = Field(description="True when the text was replayed from the cache.")
    usage: TokenUsage = Field(description="Token counts and cost of this call.")