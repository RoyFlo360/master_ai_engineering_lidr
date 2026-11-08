"""Transcript -> :class:`EstimationQuery`, the first stage of the grounded chain.

Why reformulate at all: retrieval needs a *query*, and a raw client transcript is a
bad one — long, chatty, in Spanish, and full of small talk. One cheap LLM call turns
it into something the corpus can match: the work being asked for, translated into
the corpus language, plus an honest statement of how complete the brief was.

The LLM step is advisory, never fatal. Searching the transcript itself is worse than
searching a distilled query, but it is much better than failing the request or
retrieving nothing, so every LLM failure degrades to a truncated-transcript query
with ``source="fallback"`` and a capped confidence instead of raising. The only
fatal case is a transcript with nothing in it: no fallback can turn whitespace into
a query, so that one raises :class:`ReformulationError`.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

import structlog

from app.generation.rag.errors import ReformulationError
from app.generation.rag.schemas import (
    MAX_SEARCH_QUERY_CHARS,
    MAX_SUMMARY_CHARS,
    EstimationQuery,
)

log = structlog.get_logger(__name__)

#: Model used for the reformulation call. Auxiliary and cheap by design: it distils
#: a transcript into a query, it does not estimate anything. Passed as
#: ``model_override``, so the wrapper may retry it once on ``AUX_FALLBACK_MODEL``.
DEFAULT_REFORMULATOR_MODEL = "gpt-4o-mini"

#: Prompt version under ``app/prompts/rag_reformulation/``. Bumping it is a string
#: change here, not a code refactor.
REFORMULATOR_PROMPT_VERSION = "v1"

#: Ceiling on the fallback query's confidence. A truncated transcript is a weaker
#: signal than the smallest brief worth writing, so it must never look confident.
FALLBACK_CONFIDENCE_CAP = 0.3

#: Transcript characters per unit of fallback confidence. 4,000 characters of real
#: discussion is roughly the point where the cap applies; shorter transcripts score
#: proportionally lower because they carry less to match on.
FALLBACK_CONFIDENCE_CHARS = 4000

_WHITESPACE_RE = re.compile(r"\s+")

#: Characters that occupy no width: zero-width spaces/joiners and the BOM. A
#: transcript made only of these looks non-empty to ``str.strip()`` but has nothing
#: to search for, so they are removed before the blank check.
_INVISIBLE_RE = re.compile("[\u200b-\u200d\ufeff\x00]")


def _clean(text: str) -> str:
    """Drop invisible characters, collapse whitespace runs to one space and trim.

    Transcripts arrive with newlines, tabs, speaker-formatted indentation and the
    occasional zero-width character pasted out of a document. A search string made of
    those is noisy for the lexical branch and wasteful for the embedder, so the query
    is always a single line — and a transcript of nothing but invisible characters is
    correctly recognised as empty instead of becoming a blank query.
    """
    return _WHITESPACE_RE.sub(" ", _INVISIBLE_RE.sub("", text)).strip()


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, dropping trailing whitespace."""
    return text[:limit].strip()


def _fallback_query(transcript: str) -> EstimationQuery:
    """Build a usable query from the transcript alone.

    Confidence is ``min(cap, len(transcript) / chars_per_unit)``: even a long
    transcript only proves that a lot was said, not that it was a complete brief, so
    the cap wins for anything substantial. ``summary`` and ``search_query`` are the
    same text because there is nothing else to say — the caller can tell the two
    apart by ``source == "fallback"``.
    """
    cleaned = _clean(transcript)
    confidence = min(
        FALLBACK_CONFIDENCE_CAP, len(transcript) / FALLBACK_CONFIDENCE_CHARS
    )
    return EstimationQuery(
        summary=_truncate(cleaned, MAX_SUMMARY_CHARS),
        search_query=_truncate(cleaned, MAX_SEARCH_QUERY_CHARS),
        confidence=confidence,
        source="fallback",
    )


def _normalise_query(query: EstimationQuery, *, transcript: str) -> EstimationQuery:
    """Repair the invariants the schema alone cannot enforce.

    Instructor validates shape, not intent: a model can still answer with ``" "`` as
    its search string, or with a summary it wrote but a query it forgot. Repairing
    here means the retrieval stage never has to defend against its own input.

    A missing query is *not* repaired silently into an LLM query: the replacement is
    the transcript itself, so the result is marked ``source="fallback"`` and its
    confidence is capped exactly as a failed call would be. Keeping the model's
    confidence there would let a transcript-derived query claim to be a distilled
    one, and downstream confidence and groundedness both read these fields.
    """
    search_query = _truncate(_clean(query.search_query), MAX_SEARCH_QUERY_CHARS)
    summary = _truncate(_clean(query.summary), MAX_SUMMARY_CHARS)

    if search_query:
        return query.model_copy(
            update={
                "search_query": search_query,
                "summary": summary or search_query,
                "source": "llm",
            }
        )

    fallback = _fallback_query(transcript)
    return query.model_copy(
        update={
            "search_query": fallback.search_query,
            "summary": summary or fallback.summary,
            "confidence": min(query.confidence, fallback.confidence),
            "source": "fallback",
        }
    )


def _render_prompts(
    transcript: str, *, version: str = REFORMULATOR_PROMPT_VERSION
) -> tuple[str, str]:
    """Render the versioned system/user pair from ``app/prompts``.

    The single Jinja environment is reused from ``app.prompts.loader`` rather than
    rebuilt here: prompt law routes every template through that environment
    (``StrictUndefined``, ``trim_blocks``), and a second ``Environment`` in this
    module would be free to drift from it. Imported lazily so importing this module
    stays free of the loader's schema imports.
    """
    from app.prompts.loader import _env as prompt_env

    system_prompt = prompt_env.get_template(
        f"rag_reformulation/{version}/system.j2"
    ).render()
    user_message = prompt_env.get_template(
        f"rag_reformulation/{version}/user.j2"
    ).render(transcript=transcript)
    return system_prompt, user_message


async def _query_from_llm(
    transcript: str, *, wrapper: Any, model: str
) -> EstimationQuery | None:
    """Run one structured call, or return ``None`` for any failure at all.

    ``None`` rather than an exception is the contract this module wants: the caller
    has a usable fallback for every failure mode, so raising would only mean writing
    the same ``except`` twice. The wrapper is synchronous (LiteLLM under Instructor),
    hence ``to_thread`` — it must not block the event loop.

    Prompt rendering is inside the ``try`` on purpose: it is the one dependency of
    this function that does not look like an LLM call, and a missing template or a
    ``StrictUndefined`` typo would otherwise escape as a 500 instead of degrading to
    the transcript fallback. The fallback exists for every failure, so every failure
    is routed to it here.
    """
    try:
        system_prompt, user_message = _render_prompts(transcript)
        result: Any = await asyncio.to_thread(
            wrapper.complete_structured,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=EstimationQuery,
            model_override=model,
        )
        if inspect.isawaitable(result):
            # A coroutine wrapper (an async test double, or a future async adapter)
            # is awaited rather than unpacked: unpacking it would raise and quietly
            # send a perfectly good answer down the fallback path.
            result = await result
        raw, _meta = result
    except Exception as exc:  # noqa: BLE001 - a fallback exists for every failure.
        log.warning(
            "reformulation_fallback",
            error_type=type(exc).__name__,
            transcript_chars=len(transcript),
        )
        return None

    if not isinstance(raw, EstimationQuery):
        # An injected wrapper (or a future provider adapter) can return the wrong
        # object type without raising; that is "junk" in the same sense.
        log.warning(
            "reformulation_fallback",
            error_type=type(raw).__name__,
            transcript_chars=len(transcript),
        )
        return None

    return _normalise_query(raw, transcript=transcript)


def _default_wrapper() -> Any:
    """Resolve the process-wide wrapper lazily, inside the call.

    ``lru_cache`` on the factory makes this cheap after the first request, and the
    lazy import keeps this module importable without Redis, LiteLLM keys or the rest
    of the dependency graph.
    """
    from app.dependencies import get_llm_wrapper

    return get_llm_wrapper()


async def reformulate(
    transcript: str,
    *,
    wrapper: Any | None = None,
    model: str | None = None,
) -> EstimationQuery:
    """Turn a transcript into the query the retrieval stage will run.

    Args:
        transcript: Raw client meeting transcript, in any language.
        wrapper: Object exposing the sync ``complete_structured`` API. ``None``
            resolves ``get_llm_wrapper()`` lazily; tests inject a fake.
        model: Model override for this auxiliary call. ``None`` uses
            :data:`DEFAULT_REFORMULATOR_MODEL`.

    Returns:
        The reformulated query. ``source`` says whether the LLM produced it or
        whether the transcript was used directly.

    Raises:
        ReformulationError: The transcript is empty after stripping. Nothing else
            is fatal — every other failure degrades to the fallback query.
    """
    if not _clean(transcript):
        # Checked on the cleaned text, not ``transcript.strip()``: a transcript of
        # nothing but zero-width characters would otherwise become an invisible
        # query that matches nothing and reports as a normal (non-fallback) result.
        raise ReformulationError("Cannot reformulate an empty transcript.")

    query: EstimationQuery | None = None

    # One guard around wrapper resolution *and* the call: the fallback contract is
    # "never raises unless the transcript is empty", so anything the stage can throw
    # — a missing template, an unbuildable wrapper, a provider timeout — has to land
    # here rather than on the caller.
    try:
        resolved_wrapper = wrapper if wrapper is not None else _default_wrapper()
        query = await _query_from_llm(
            transcript,
            wrapper=resolved_wrapper,
            model=model or DEFAULT_REFORMULATOR_MODEL,
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the request.
        log.warning(
            "reformulation_fallback",
            error_type=type(exc).__name__,
            transcript_chars=len(transcript),
        )

    if query is None:
        query = _fallback_query(transcript)

    log.info(
        "reformulation_done",
        source=query.source,
        confidence=round(query.confidence, 3),
        search_query_len=len(query.search_query),
    )
    return query
