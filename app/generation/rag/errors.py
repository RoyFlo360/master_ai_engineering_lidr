"""Domain errors for the grounded-estimation chain (T0B).

Split from ``retrieval/errors.py`` on purpose: retrieval failures are transport
problems the retrieval router maps to 500/502, while these are *chain* failures
the estimate router maps differently (a reformulation failure degrades to a
fallback query, a validation failure retries once, and only a generation failure
is fatal).

Each class carries the HTTP status its own router should use, so the mapping
lives next to the error rather than in a router that has to remember it.
"""

from __future__ import annotations


class ChainError(RuntimeError):
    """Base class for every failure in reformulate → retrieve → assemble → generate."""

    status_code: int = 500
    public_message: str = "Failed to run the estimation pipeline."


class ReformulationError(ChainError):
    """The LLM could not turn the transcript into an ``EstimationQuery``.

    Degradable: the reformulator falls back to a truncated transcript as the
    search string with ``confidence`` capped low. The chain only raises this when
    even the fallback cannot produce a usable query (e.g. an empty transcript
    after cleaning).
    """

    status_code = 502
    public_message = "Could not interpret the transcript."


class ContextAssemblyError(ChainError):
    """Retrieved chunks could not be turned into prompt context (empty input)."""

    status_code = 422
    public_message = "No context available to build an estimate."


class GenerationError(ChainError):
    """The generation call failed, or returned something that is not an estimate."""

    status_code = 502
    public_message = "The estimation model is unavailable. Please retry later."


class CitationValidationError(ChainError):
    """The estimate cited a chunk it was not shown, and the retry did not fix it.

    Raised only after the single corrective retry in ``validation.py`` has been
    used: a second invention is not a flake, it is a broken prompt.
    """

    status_code = 502
    public_message = "The estimate could not be grounded in the retrieved evidence."
