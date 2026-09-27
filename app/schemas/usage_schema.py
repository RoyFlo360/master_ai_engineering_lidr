"""Response shape for the running usage totals the sidebar reports."""

from pydantic import BaseModel, Field


class UsageTotalsResponse(BaseModel):
    """What this API process has spent since it started.

    Process-local by design: the totals are wiped by a restart, and in
    development by every uvicorn ``--reload`` after a code change.
    """

    calls: int = Field(description="Requests served, cache hits included.")
    cache_hits: int = Field(description="Requests answered from the cache, at no cost.")
    input_tokens: int = Field(description="Prompt tokens across every call.")
    output_tokens: int = Field(description="Completion tokens across every call.")
    total_tokens: int = Field(description="Input plus output tokens across every call.")
    cost_usd: float = Field(description="Billed cost of every call, in USD.")
