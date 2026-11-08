"""Citation validation: an estimate may only cite chunks it was actually shown.

Why this is a separate module: the check is pure and cheap, so it can be tested
without an LLM, a database or an HTTP client. The *policy* on a failure is not
pure — it costs a provider call — so it lives in the orchestrator instead:

1. ``check_citations`` reports the offending ids.
2. The orchestrator regenerates **once**, passing those ids to the prompt
   (``estimator.generate_estimate(..., invalid_citation_ids=...)``).
3. If the second estimate still cites something unknown,
   ``CitationValidationError`` is raised. A second invention is not a flake, it
   is a broken prompt, and returning it as if it were grounded would defeat the
   point of the flag.

``strip_unknown`` is the non-retrying alternative: drop the unsupported claims
and mark the estimate ungrounded when nothing traceable is left.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.generation.rag.schemas import GroundedEstimate


class CitationCheck(BaseModel):
    """Result of comparing an estimate's citations with the retrieved ids."""

    ok: bool = Field(description="True when every citation is traceable.")
    unknown_ids: list[int] = Field(
        default_factory=list,
        description="Cited ids that were not in the retrieved set, deduplicated "
        "and sorted. Empty when ``ok`` is True.",
    )
    message: str = Field(description="Human-readable reason, safe to log.")


def _split_invalid(
    estimate: GroundedEstimate, allowed_ids: set[int]
) -> tuple[list[int], list[str]]:
    """Split citations into (unknown integer ids, malformed raw values).

    A ``chunk_id`` that is not a real integer cannot be traceable to a chunk, so
    it is just as invalid as an id that was never retrieved. It is reported
    separately because ``unknown_ids`` is typed ``list[int]`` and a malformed
    value has no honest integer form.
    """
    unknown: set[int] = set()
    malformed: list[str] = []

    for citation in getattr(estimate, "citations", None) or []:
        raw = getattr(citation, "chunk_id", None)
        # ``bool`` is an ``int`` subclass in Python; ``True`` is not a chunk id.
        if isinstance(raw, bool) or not isinstance(raw, int):
            malformed.append(repr(raw))
            continue
        if raw not in allowed_ids:
            unknown.add(raw)

    return sorted(unknown), malformed


def check_citations(estimate: GroundedEstimate, allowed_ids: set[int]) -> CitationCheck:
    """Verify every citation points at a chunk that was retrieved.

    Args:
        estimate: The generated estimate.
        allowed_ids: Ids actually present in the prompt context
            (``context_assembler.sources_of(chunks)``).

    Returns:
        A ``CitationCheck``; ``ok=False`` carries the offending ids so the
        orchestrator can name them in the corrective retry.
    """
    unknown, malformed = _split_invalid(estimate, allowed_ids)

    if malformed:
        return CitationCheck(
            ok=False,
            unknown_ids=unknown,
            message=(
                "Citations carried non-integer chunk ids: " + ", ".join(malformed)
            ),
        )
    if unknown:
        return CitationCheck(
            ok=False,
            unknown_ids=unknown,
            message=(
                "Citations reference chunk ids that were not in the retrieved "
                "context: " + ", ".join(str(chunk_id) for chunk_id in unknown)
            ),
        )
    return CitationCheck(ok=True, unknown_ids=[], message="All citations are traceable.")


def strip_unknown(estimate: GroundedEstimate, allowed_ids: set[int]) -> GroundedEstimate:
    """Drop untraceable citations without calling the model again.

    Returns the same object when every citation is valid. Otherwise returns a
    copy with the offending citations removed; when none remain, ``grounded`` is
    set to ``False`` because no claim in the estimate is supported by evidence
    any more. ``modules`` and ``total_hours`` are left untouched: this function
    removes unsupported evidence, it does not silently re-estimate.
    """
    unknown, malformed = _split_invalid(estimate, allowed_ids)
    if not unknown and not malformed:
        return estimate

    kept = [
        citation
        for citation in estimate.citations
        if isinstance(getattr(citation, "chunk_id", None), int)
        and not isinstance(getattr(citation, "chunk_id", None), bool)
        and citation.chunk_id in allowed_ids
    ]
    return estimate.model_copy(
        update={"citations": kept, "grounded": bool(kept) and estimate.grounded}
    )
