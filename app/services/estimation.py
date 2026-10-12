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
from app.schemas.critic import CriticFeedback
from app.schemas.new_estimation_schema import (
    ACBResponse,
    DetailLevel,
    EstimationRequest,
    EstimationResult,
    OutputFormat,
    ProjectType,
    StructuredEstimationResponse,
    TurnObservation,
)
from app.services.boss import Boss
from app.services.cache import EstimationCache
from app.services.critic import Critic
from app.services.llm_wrapper import LLMWrapper
from app.services.semantic_cache import EstimationSemanticCache
from app.sessions.compression import apply_compression
from app.sessions.metadata_extractor import update_metadata
from app.sessions.models import Session
from app.sessions.tier_resolver import Tier, resolve_tier

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
        conversational_prompt_version: str = "v3",
        metadata_extractor_model: str = "gpt-4o-mini",
        compression_model: str = "gpt-4o-mini",
        anchor_detection_mode: str = "heuristic",
        critic_model: str = "gpt-4o-mini",
        boss_max_iterations: int = 3,
        enable_llm_metadata_fallback: bool = True,
    ) -> None:
        self.llm_wrapper = llm_wrapper
        self.exact_cache = exact_cache
        self.semantic_cache = semantic_cache
        self.openai_client = openai_client
        self.prompt_version = prompt_version
        self.conversational_prompt_version = conversational_prompt_version
        self.metadata_extractor_model = metadata_extractor_model
        self.compression_model = compression_model
        self.anchor_detection_mode = anchor_detection_mode
        self.critic_model = critic_model
        self.boss_max_iterations = boss_max_iterations
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
        tier: Tier | None = None,
        attachments_total_chars: int = 0,
    ) -> StructuredEstimationResponse:
        """Multi-turn estimation pipeline (Session 5/6).

        Differences with ``estimate``:
        - No exact/semantic caching: every turn depends on the conversation
          history + metadata, so two identical transcripts in different
          sessions are NOT the same call. ``cache_hit`` is always ``False``.
        - The system prompt is the versioned conversational template (v3 by
          default), which embeds the current ``ProjectMetadata`` block, the
          resolved ``<audience>`` tier and, on an ACB iteration, the critic
          feedback. The LLM also receives the prior user/assistant turns.
        - After validation, compression runs (anchor promotion + cumulative
          summary + sliding window), then ``ProjectMetadata`` is refreshed.
        """
        # 1. Input guardrail on the enriched transcript (the caller has
        #    already concatenated any extracted attachment text into it).
        check_input(transcript, openai_client=self.openai_client)

        # 2. Resolve the audience tier. Override (when the caller passed one)
        #    wins; otherwise the rule chain decides from transcript + metadata.
        resolved_tier, rule = resolve_tier(
            transcript=transcript,
            metadata=session.metadata,
            override=tier,
        )
        session.last_resolved_tier = resolved_tier.value
        session.last_tier_rule = rule

        # 3. Render the conversational system + user prompts. v3 carries the
        #    <audience> block driven by the resolved tier; v2 ignores it.
        system_prompt, user_message = render_conversational_prompt(
            description=transcript,
            project_type=project_type,
            detail_level=detail_level,
            output_format=output_format,
            metadata=session.metadata,
            version=self.conversational_prompt_version,
            tier=resolved_tier,
        )

        # 4. Build the messages array: fresh system + composed history
        #    (summary + anchors + sliding window) + current user.
        messages: list[dict[str, str]] = session.history.to_messages(
            system_prompt=system_prompt
        )
        messages.append({"role": "user", "content": user_message})

        log.info(
            "estimation_conversational_request",
            session_id=session.session_id,
            tier=resolved_tier.value,
            tier_rule=rule,
            history_messages=len(session.history.messages),
            metadata_is_empty=session.metadata.is_empty(),
            transcript_chars=len(transcript),
        )

        # 5. LLM call with Instructor + Pydantic validators.
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

        # 6. Output guardrail (filter policy: normalises low-confidence answers).
        result = enforce_scope_response(result)

        # 7. Append the turn, then compress: anchor promotion + cumulative
        #    summary + sliding window. The turn index is a true per-session
        #    counter (NOT ``len(messages)//2`` — that saturates at the window
        #    cap and would mislabel every turn past ``max_turns``).
        session.history.append(user=user_message, assistant=result.model_dump_json())
        session.turns_seen += 1
        turn_index = session.turns_seen
        apply_compression(
            session.history,
            llm_wrapper=self.llm_wrapper,
            compression_model=self.compression_model,
            anchor_detection_mode=self.anchor_detection_mode,
        )

        # 8. Refresh ProjectMetadata: heuristic first, LLM only if the heuristic
        #    found no structured facts. Failures are swallowed inside.
        session.metadata = update_metadata(
            previous=session.metadata,
            transcript=transcript,
            result=result,
            llm_wrapper=self.llm_wrapper,
            model=self.metadata_extractor_model,
            enable_llm_fallback=self.enable_llm_metadata_fallback,
        )

        # 9. Emit the unified per-turn observation. A single structured event
        #    (rather than several log lines) makes the stress runner trivial: it
        #    reads ``response.observation`` straight from the JSON.
        observation = TurnObservation(
            turn_index=max(1, turn_index),
            session_id=session.session_id,
            enriched_transcript_chars=len(transcript),
            attachments_total_chars=attachments_total_chars,
            messages_in_window=len(session.history.messages),
            anchors_count=len(session.history.anchors),
            summary_chars=len(session.history.summary or ""),
            tokens_in=int(meta.get("tokens_in", 0) or 0),
            tokens_out=int(meta.get("tokens_out", 0) or 0),
            cost_usd=float(meta.get("cost_usd", 0.0) or 0.0),
            latency_ms=int(meta.get("latency_ms", 0) or 0),
            cache_hit_kind="none",
            last_resolved_tier=session.last_resolved_tier,
        )
        log.info("turn_observed", **observation.model_dump())

        return StructuredEstimationResponse(
            result=result,
            prompt_version=self.conversational_prompt_version,
            cache_hit=False,
            observation=observation,
        )

    def estimate_with_acb(
        self,
        *,
        session: Session,
        transcript: str,
        project_type: ProjectType,
        detail_level: DetailLevel,
        output_format: OutputFormat,
        tier: Tier | None = None,
    ) -> ACBResponse:
        """Actor-Critic-Boss variant of the conversational pipeline.

        The session is updated **only** with the final Boss-approved (or
        Boss-synthesized) result — intermediate actor drafts are throwaway.
        That keeps the conversation state coherent: from the user's point of
        view, the turn produced exactly one assistant message.
        """
        # 1. Input guardrail.
        check_input(transcript, openai_client=self.openai_client)

        # 2. Resolve tier (same as the actor path).
        resolved_tier, rule = resolve_tier(
            transcript=transcript,
            metadata=session.metadata,
            override=tier,
        )
        session.last_resolved_tier = resolved_tier.value
        session.last_tier_rule = rule

        log.info(
            "estimation_acb_request",
            session_id=session.session_id,
            tier=resolved_tier.value,
            tier_rule=rule,
            transcript_chars=len(transcript),
        )

        # 3. The actor callable re-renders the prompt each iteration so critic
        #    feedback is woven in; the output guardrail runs on every draft.
        def _actor(critic_feedback: CriticFeedback | None) -> EstimationResult:
            system_prompt, user_message = render_conversational_prompt(
                description=transcript,
                project_type=project_type,
                detail_level=detail_level,
                output_format=output_format,
                metadata=session.metadata,
                version=self.conversational_prompt_version,
                tier=resolved_tier,
                critic_feedback=critic_feedback,
            )
            messages: list[dict[str, str]] = session.history.to_messages(
                system_prompt=system_prompt
            )
            messages.append({"role": "user", "content": user_message})

            draft, meta = self.llm_wrapper.complete_structured_chat(
                messages=messages,
                response_model=EstimationResult,
            )
            log.info(
                "acb_actor_draft",
                with_critic_feedback=critic_feedback is not None,
                issues_in_feedback=(
                    len(critic_feedback.issues) if critic_feedback is not None else 0
                ),
                confidence_pct=draft.confidence_pct,
                total_cost_eur=draft.total_cost_eur,
                **meta,
            )
            return enforce_scope_response(draft)

        # 4. Critic callable.
        critic = Critic(llm_wrapper=self.llm_wrapper, model=self.critic_model)

        def _critic(draft: EstimationResult) -> CriticFeedback:
            return critic.review(
                transcript=transcript,
                metadata=session.metadata,
                tier=resolved_tier,
                result=draft,
            )

        # 5. Boss orchestrates.
        boss = Boss(max_iterations=self.boss_max_iterations)
        final_result, trace = boss.run(actor=_actor, critic=_critic)

        # 6. Persist the final result into the session (single turn append).
        session.history.append(
            user=transcript, assistant=final_result.model_dump_json()
        )
        session.turns_seen += 1
        apply_compression(
            session.history,
            llm_wrapper=self.llm_wrapper,
            compression_model=self.compression_model,
            anchor_detection_mode=self.anchor_detection_mode,
        )

        # 7. Refresh metadata from the final result.
        session.metadata = update_metadata(
            previous=session.metadata,
            transcript=transcript,
            result=final_result,
            llm_wrapper=self.llm_wrapper,
            model=self.metadata_extractor_model,
            enable_llm_fallback=self.enable_llm_metadata_fallback,
        )

        return ACBResponse(
            result=final_result,
            prompt_version=self.conversational_prompt_version,
            cache_hit=False,
            acb=trace,
        )
