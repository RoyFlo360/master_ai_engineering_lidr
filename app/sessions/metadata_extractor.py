"""Metadata refresh for the conversational pipeline.

The primary path is the cheap :func:`app.sessions.heuristics.extract_metadata_heuristic`
(no extra LLM call). The LLM extractor is kept as a **fallback**: it only runs when
the heuristic finds no structured facts at all (no project name, team size or
technology) for the turn, and ``enable_llm_fallback`` is true. That keeps the
common turn at one billed call while still leaning on the model when the text is
ambiguous or non-Latin.

Both paths merge with the prior metadata (scalar overwrite + tech list union) and
swallow their own failures, so a bad turn never freezes the conversation.
"""

from __future__ import annotations

from structlog import get_logger

from app.prompts.loader import render_metadata_extraction_prompt
from app.schemas.new_estimation_schema import EstimationResult
from app.services.llm_wrapper import LLMWrapper
from app.sessions.heuristics import extract_metadata_heuristic
from app.sessions.models import ProjectMetadata

log = get_logger()


def _found_structured_facts(update: ProjectMetadata) -> bool:
    """Whether the heuristic produced anything beyond the always-set scope."""
    return bool(
        update.project_name or update.assumed_team_size or update.mentioned_technologies
    )


def update_metadata(
    *,
    previous: ProjectMetadata,
    transcript: str,
    result: EstimationResult,
    llm_wrapper: LLMWrapper,
    model: str,
    enable_llm_fallback: bool = True,
) -> ProjectMetadata:
    """Refresh metadata: heuristic first, LLM only when the heuristic is thin."""
    heuristic_update = extract_metadata_heuristic(transcript=transcript, result=result)
    merged = previous.merge_with(heuristic_update)
    log.info(
        "metadata_heuristic_completed",
        project_name=merged.project_name,
        team_size=merged.assumed_team_size,
        tech_count=len(merged.mentioned_technologies),
        found_structured_facts=_found_structured_facts(heuristic_update),
    )

    if not enable_llm_fallback or _found_structured_facts(heuristic_update):
        return merged

    log.info("metadata_llm_fallback", reason="heuristic_found_no_structured_facts")
    return _update_metadata_via_llm(
        previous=merged,
        transcript=transcript,
        result=result,
        llm_wrapper=llm_wrapper,
        model=model,
    )


def _update_metadata_via_llm(
    *,
    previous: ProjectMetadata,
    transcript: str,
    result: EstimationResult,
    llm_wrapper: LLMWrapper,
    model: str,
) -> ProjectMetadata:
    """Fallback extractor: a narrow second LLM call returning ``ProjectMetadata``.

    On failure: log + return ``previous`` so a single bad extraction doesn't
    cascade and freeze the session.
    """
    system_prompt, user_message = render_metadata_extraction_prompt(
        transcript=transcript,
        result=result,
        previous=previous,
    )

    try:
        extracted, meta = llm_wrapper.complete_structured_chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_model=ProjectMetadata,
            model_override=model,
            max_tokens=1000,
            max_retries=2,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "metadata_extraction_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return previous

    merged = previous.merge_with(extracted)
    log.info(
        "metadata_extraction_completed",
        model=meta["model"],
        latency_ms=meta["latency_ms"],
        project_name=merged.project_name,
        team_size=merged.assumed_team_size,
        tech_count=len(merged.mentioned_technologies),
    )
    return merged
