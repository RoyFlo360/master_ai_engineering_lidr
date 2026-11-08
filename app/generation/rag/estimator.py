"""Grounded estimate generation for the T0B chain.

This is the only module in the estimation chain that talks to an LLM. It exists
so the rest of the chain — reformulate, retrieve, assemble, validate — can be
tested without a provider: a test injects a fake ``wrapper`` and never leaves the
process.

Two behaviours are load-bearing:

* **No context, no call.** When ``context`` is empty there is nothing to ground
  an estimate in, so the function returns an explicit ungrounded estimate and
  never spends a token. ``grounded`` is not decoration: it is what tells a
  caller the answer is not evidence-backed.
* **The prompt lives in ``app/prompts``**, not in this file. The corrective retry
  and the citation ids are passed to the template as data, so no prompt text is
  ever assembled in Python (rules/development.md, "Prompt law").
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.generation.rag.errors import GenerationError
from app.generation.rag.schemas import EstimationQuery, GroundedEstimate
from app.prompts.loader import _env as _prompt_env

log = structlog.get_logger(__name__)

#: Version directory under ``app/prompts/rag_grounded_estimation/``.
PROMPT_VERSION = "v1"

#: The summary returned when the corpus produced nothing usable. One constant so
#: the orchestrator and a direct ``generate_estimate`` call cannot disagree.
NO_EVIDENCE_SUMMARY = (
    "No projects in the corpus matched this brief, so there is no retrieved "
    "evidence to ground an estimate in. No modules, hours or citations are "
    "reported, and this answer must not be presented as evidence-backed."
)


def unavailable_estimate() -> GroundedEstimate:
    """The honest answer when nothing was retrieved.

    Kept here rather than in the orchestrator so the chain has exactly one
    definition of "ungrounded": the same shape ``generate_estimate`` returns for
    an empty context, and the same shape the orchestrator returns when
    retrieval finds nothing.
    """
    return GroundedEstimate(
        summary=NO_EVIDENCE_SUMMARY,
        modules=[],
        total_hours=0,
        citations=[],
        confidence_pct=0,
        grounded=False,
    )


def _render(role: str, **context: Any) -> str:
    """Render one T0B template with the loader's environment.

    The loader's own Jinja environment is reused deliberately: ``StrictUndefined``
    (a typo in a template variable must fail at render time, not ship an empty
    instruction) plus ``trim_blocks``/``lstrip_blocks`` are prompt-law
    conventions, and a second environment here would be free to drift.
    """
    template = f"rag_grounded_estimation/{PROMPT_VERSION}/{role}.j2"
    return _prompt_env.get_template(template).render(**context)


def build_messages(
    query: EstimationQuery,
    context: str,
    *,
    invalid_citation_ids: list[int] | None = None,
    invalid_citation_note: str | None = None,
) -> tuple[str, str]:
    """Render ``(system_prompt, user_message)`` for one generation turn.

    ``invalid_citation_ids`` is the corrective-retry channel: the orchestrator
    passes the ids that failed validation and the template names them, so the
    model is told what to fix instead of being asked to try again blindly.

    ``invalid_citation_note`` carries the same feedback when the offending
    citation was *malformed* (a non-integer ``chunk_id``) and therefore has no
    integer form to name. Both are always passed to the template, which tests
    for emptiness — the renderer runs under ``StrictUndefined``, so an absent
    variable would raise rather than read as false.
    """
    system_prompt = _render("system")
    user_message = _render(
        "user",
        query=query,
        context=context,
        invalid_citation_ids=invalid_citation_ids,
        invalid_citation_note=invalid_citation_note,
    )
    return system_prompt, user_message


async def generate_estimate(
    query: EstimationQuery,
    context: str,
    *,
    source_ids: set[int],
    wrapper: Any = None,
    model: str | None = None,
    invalid_citation_ids: list[int] | None = None,
    invalid_citation_note: str | None = None,
) -> GroundedEstimate:
    """Ask the model for an estimate grounded only in ``context``.

    Args:
        query: The reformulated brief. Its ``summary``/``search_query`` are what
            the prompt shows as ``<project_brief>``.
        context: Assembled ``<source>`` blocks, or an empty string.
        source_ids: Chunk ids present in ``context``. Logged here; the
            authoritative citation check lives in ``validation.py``.
        wrapper: ``LLMWrapper``-compatible client. ``None`` resolves the
            process singleton from ``app.dependencies`` at call time, so tests
            can inject a fake without importing the provider stack.
        model: Per-call model override, forwarded as ``model_override``.
        invalid_citation_ids: Ids named in the corrective retry prompt.
        invalid_citation_note: Free-text form of the same feedback, used when the
            offending citation was malformed rather than merely unknown.

    Returns:
        A ``GroundedEstimate`` whose ``grounded`` flag reflects whether any
        context reached the prompt.

    Raises:
        GenerationError: the model call failed, or returned something that is
            not a ``GroundedEstimate``.
    """
    if not context.strip():
        # The whole point of ``grounded``: do not ask a model to answer from
        # memory when retrieval found nothing. This also means an empty corpus
        # costs nothing.
        log.info(
            "estimate_generated",
            modules=0,
            citations=0,
            total_hours=0,
            grounded=False,
            reason="empty_context",
        )
        return unavailable_estimate()

    if wrapper is None:
        # Imported inside the function so this module stays importable without
        # the OpenAI/LiteLLM stack, and so ``app.dependencies`` stays the single
        # place a test has to patch.
        from app.dependencies import get_llm_wrapper

        wrapper = get_llm_wrapper()

    system_prompt, user_message = build_messages(
        query,
        context,
        invalid_citation_ids=invalid_citation_ids,
        invalid_citation_note=invalid_citation_note,
    )

    try:
        # ``complete_structured`` is synchronous (Instructor/LiteLLM blocking
        # call); one call per request, so off the event loop rather than adding
        # an async client that the wrapper does not have. ``model_override`` is
        # only sent when the caller asked for a specific model, so a minimal
        # wrapper (or a test fake) that has no model knob still works.
        call_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "user_message": user_message,
            "response_model": GroundedEstimate,
        }
        if model is not None:
            call_kwargs["model_override"] = model
        result, _meta = await asyncio.to_thread(wrapper.complete_structured, **call_kwargs)
    except Exception as exc:  # noqa: BLE001 - every provider failure is the same 502.
        log.error(
            "estimate_generation_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise GenerationError("The estimation model call failed.") from exc

    if not isinstance(result, GroundedEstimate):
        log.error(
            "estimate_generation_unusable",
            returned_type=type(result).__name__,
        )
        raise GenerationError("The estimation model returned an unusable estimate.")

    # ``grounded`` is the caller's fact, not the model's opinion: context was
    # non-empty, so the estimate was produced from retrieved evidence.
    estimate = result.model_copy(update={"grounded": True})

    log.info(
        "estimate_generated",
        modules=len(estimate.modules),
        citations=len(estimate.citations),
        total_hours=estimate.total_hours,
        grounded=estimate.grounded,
        sources=len(source_ids),
        correction=bool(invalid_citation_ids),
    )
    return estimate
