"""Pipeline orchestrator. Glue between guardrails, caches, prompt rendering and
the LLM wrapper. The router holds none of this logic — its only job is to
translate HTTP errors.

Pipeline (Session 4, final):

    1. Input guardrails (moderation + prompt injection + PII heuristics)
    2. Exact-match cache lookup  → return cache_hit=True on hit
    3. Semantic cache lookup     → return cache_hit=True on hit
    4. Render the versioned prompt
    5. LLM call via Instructor with response_model=EstimationResult
    6. Output guardrail (enforce_scope_response, filter policy)
    7. Write to BOTH caches (exact + semantic)
    8. Return EstimationResponse with cache_hit=False

Order rationale: guardrails go before any cache because a malicious or PII
description should never be served from cache. The exact-match cache goes
before the semantic cache because it's the cheapest (no embedding call). The
semantic cache write happens AFTER output validation so we never cache failed
estimations.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog

from app.guardrails.input import check_input
from app.guardrails.output import enforce_scope_response
from app.prompts import render_estimation_prompt
from app.prompts.loader import render_conversational_prompt
from app.schemas.new_estimation_schema import (
    DetailLevel,
    EstimationRequest,
    EstimationResult,
    OutputFormat,
    ProjectType,
    StructuredEstimationResponse,
)
from app.services.cache import EstimationCache
from app.services.llm_wrapper import LLMWrapper
from app.services.semantic_cache import EstimationSemanticCache
from app.sessions.metadata_extractor import update_metadata
from app.sessions.models import Session

log = structlog.get_logger()


def _exact_cache_key(request: EstimationRequest, prompt_version: str, model: str) -> str:
    """Deterministic SHA-256 key over the typed request + prompt_version + model."""
    payload = json.dumps(
        {
            "description": request.description,
            "project_type": request.project_type.value,
            "detail_level": request.detail_level.value,
            "output_format": request.output_format.value,
            "prompt_version": prompt_version,
            "model": model,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"estimation:v2:{digest}"


class EstimationService:
    """Single entry point for the structured estimation pipeline."""

    def __init__(
        self,
        *,
        llm_wrapper: LLMWrapper,
        exact_cache: EstimationCache,
        semantic_cache: EstimationSemanticCache | None = None,
        openai_client: Any | None = None,
        prompt_version: str = "v1",
        conversational_prompt_version: str = "v2",
        metadata_extractor_model: str = "gpt-4o-mini",
        enable_llm_metadata_fallback: bool = True,
    ) -> None:
        self.llm_wrapper = llm_wrapper
        self.exact_cache = exact_cache
        self.semantic_cache = semantic_cache
        self.openai_client = openai_client
        self.prompt_version = prompt_version
        self.conversational_prompt_version = conversational_prompt_version
        self.metadata_extractor_model = metadata_extractor_model
        self.enable_llm_metadata_fallback = enable_llm_metadata_fallback

    def estimate(self, request: EstimationRequest) -> StructuredEstimationResponse:
        # 1. Input guardrails — raises InputGuardrailViolation on rejection.
        check_input(request.description, openai_client=self.openai_client)

        # 2. Exact-match cache lookup.
        cache_key = _exact_cache_key(
            request, self.prompt_version, self.llm_wrapper.primary_model
        )
        cached = self.exact_cache.get(cache_key)
        if cached:
            log.info("estimation_cache_hit", kind="exact", key_prefix=cache_key[:24])
            result = EstimationResult.model_validate(cached["result"])
            return StructuredEstimationResponse(
                result=result, prompt_version=self.prompt_version, cache_hit=True
            )

        # 3. Semantic cache lookup.
        if self.semantic_cache is not None:
            semantic_hit = self.semantic_cache.lookup(request, self.prompt_version)
            if semantic_hit is not None:
                log.info("estimation_cache_hit", kind="semantic")
                return StructuredEstimationResponse(
                    result=semantic_hit,
                    prompt_version=self.prompt_version,
                    cache_hit=True,
                )

        # 4. Render the versioned prompt.
        system_prompt, user_message = render_estimation_prompt(
            request, version=self.prompt_version
        )

        # 5. LLM call with Instructor + Pydantic validators (re-prompts on failure).
        result, meta = self.llm_wrapper.complete_structured(
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=EstimationResult,
        )
        log.info(
            "estimation_generated",
            prompt_version=self.prompt_version,
            confidence_pct=result.confidence_pct,
            total_cost_eur=result.total_cost_eur,
            phases=len(result.phases),
            **meta,
        )

        # 6. Output guardrail (filter): normalises low-confidence answers.
        result = enforce_scope_response(result)

        # 7. Cache the validated payload only (never persist failed validations).
        self.exact_cache.set(
            cache_key,
            {
                "result": result.model_dump(mode="json"),
                "prompt_version": self.prompt_version,
            },
        )
        if self.semantic_cache is not None:
            self.semantic_cache.store(request, result, self.prompt_version)

        # 8. Return.
        return StructuredEstimationResponse(
            result=result, prompt_version=self.prompt_version, cache_hit=False
        )

    def estimate_conversational(
        self,
        *,
        session: Session,
        transcript: str,
        project_type: ProjectType,
        detail_level: DetailLevel,
        output_format: OutputFormat,
    ) -> StructuredEstimationResponse:
        """Multi-turn estimation pipeline (Session 5).

        Differences with ``estimate``:
        - No exact/semantic caching: every turn depends on the conversation
          history + metadata, so two identical transcripts in different
          sessions are NOT the same call. ``cache_hit`` is always ``False``.
        - The system prompt is the v2 template, which embeds the current
          ``ProjectMetadata`` block. The LLM also receives the prior
          user/assistant turns.
        - After validation, the session's history is appended and
          ``ProjectMetadata`` is refreshed (heuristic, with an LLM fallback).
        """
        # 1. Input guardrail on the enriched transcript (the caller has
        #    already concatenated any extracted attachment text into it).
        check_input(transcript, openai_client=self.openai_client)

        # 2. Render the conversational system + user prompts (v2 includes the
        #    <project_metadata> block).
        system_prompt, user_message = render_conversational_prompt(
            description=transcript,
            project_type=project_type,
            detail_level=detail_level,
            output_format=output_format,
            metadata=session.metadata,
            version=self.conversational_prompt_version,
        )

        # 3. Build the messages array: fresh system + prior history (already
        #    bounded by the sliding window) + current user. The system prompt is
        #    regenerated from the current metadata and prepended by to_messages().
        messages: list[dict[str, str]] = session.history.to_messages(
            system_prompt=system_prompt
        )
        messages.append({"role": "user", "content": user_message})

        log.info(
            "estimation_conversational_request",
            session_id=session.session_id,
            history_messages=len(session.history.messages),
            metadata_is_empty=session.metadata.is_empty(),
            transcript_chars=len(transcript),
        )

        # 4. LLM call with Instructor + Pydantic validators.
        result, meta = self.llm_wrapper.complete_structured_chat(
            messages=messages,
            response_model=EstimationResult,
        )
        log.info(
            "estimation_conversational_generated",
            session_id=session.session_id,
            confidence_pct=result.confidence_pct,
            total_cost_eur=result.total_cost_eur,
            phases=len(result.phases),
            **meta,
        )

        # 5. Output guardrail (filter policy: normalises low-confidence answers).
        result = enforce_scope_response(result)

        # 6. Append the turn to the history (sliding window auto-trims).
        session.history.append(user=user_message, assistant=result.model_dump_json())

        # 7. Refresh ProjectMetadata: heuristic first, LLM only if the heuristic
        #    found no structured facts. Failures are swallowed inside.
        session.metadata = update_metadata(
            previous=session.metadata,
            transcript=transcript,
            result=result,
            llm_wrapper=self.llm_wrapper,
            model=self.metadata_extractor_model,
            enable_llm_fallback=self.enable_llm_metadata_fallback,
        )

        return StructuredEstimationResponse(
            result=result,
            prompt_version=self.conversational_prompt_version,
            cache_hit=False,
        )
