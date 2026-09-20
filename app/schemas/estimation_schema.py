from pydantic import BaseModel, Field


class EstimationRequest(BaseModel):
    """Incoming request containing a meeting transcription to estimate."""

    transcription: str = Field(..., min_length=50, description="Meeting transcription text")


class TokenUsage(BaseModel):
    """Token consumption details from the LLM call."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float = Field(..., description="LLM cost")


class EstimationResponse(BaseModel):
    """Response containing the generated estimation and metadata."""

    estimation: str = Field(..., description="Generated software estimation in markdown")
    model: str = Field(..., description="LLM model used")
    provider: str = Field(..., description="LLM provider used")
    usage: TokenUsage


class StreamEstimationRequest(BaseModel):
    """Streaming endpoint request.

    ``usage`` deliberately has no field here: token counts and cost are produced by
    the LLM call, so they belong in the response, never in the request.
    """

    transcription: str = Field(..., min_length=50, description="Meeting transcription text")
    model: str | None = Field(default=None, description="Override the default model")
    max_tokens: int | None = Field(
        default=None,
        gt=0,
        le=16000,
        description="Output cap; omit to use the service default",
    )
