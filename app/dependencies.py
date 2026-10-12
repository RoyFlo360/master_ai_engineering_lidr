"""FastAPI dependency factories for shared singletons (cache, LLM wrapper, ledger)."""

from __future__ import annotations

from functools import lru_cache

from openai import OpenAI
from redis import from_url
from structlog import get_logger

from app.config import get_settings
from app.services.cache import EstimationCache
from app.services.estimation import EstimationService
from app.services.llm_wrapper import LLMWrapper
from app.services.semantic_cache import EstimationSemanticCache, redisearch_available
from app.services.usage_ledger import UsageLedger
from app.sessions.store import SessionStore

log = get_logger()


@lru_cache
def get_cache() -> EstimationCache:
    settings = get_settings()
    return EstimationCache.from_url(settings.REDIS_URL, ttl=settings.CACHE_TTL)


@lru_cache
def get_llm_wrapper() -> LLMWrapper:
    settings = get_settings()
    return LLMWrapper(
        deepseek_api_key=settings.DEEPSEEK_API_KEY,
        openai_api_key=settings.OPENAI_API_KEY,
        anthropic_api_key=settings.ANTHROPIC_API_KEY,
        primary_model=settings.PRIMARY_MODEL,
        fallback_model=settings.FALLBACK_MODEL,
        timeout=settings.LLM_TIMEOUT,
        num_retries=settings.LLM_RETRIES,
        max_tokens=settings.MAX_OUTPUT_TOKENS,
        enable_thinking=settings.ENABLE_THINKING,
        cache=get_cache(),
        aux_fallback_model=settings.AUX_FALLBACK_MODEL,
    )


@lru_cache
def get_usage_ledger() -> UsageLedger:
    """One ledger per process, shared by every request it serves."""
    return UsageLedger()


@lru_cache
def get_openai_client() -> OpenAI | None:
    """Lazy OpenAI client used by ``check_input`` (Moderation API) and the
    semantic cache (Embeddings API)."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        return None
    return OpenAI(api_key=settings.OPENAI_API_KEY)


@lru_cache
def get_semantic_cache() -> EstimationSemanticCache | None:
    """Build the semantic cache, swallowing setup errors so the rest of the
    pipeline keeps working if Redis Stack / RediSearch is not available
    (e.g. running on vanilla redis:7-alpine)."""
    settings = get_settings()
    openai_client = get_openai_client()
    if openai_client is None:
        log.warning("semantic_cache_disabled", reason="no_openai_key")
        return None

    # We use redisvl's OpenAITextVectorizer; it lazy-loads the OpenAI client.
    try:
        redis_client = from_url(settings.REDIS_URL, decode_responses=False)
        # Check RediSearch *before* building the embedding client: the vectorizer
        # probes OpenAI for the model dimensions at construction, and a vanilla
        # Redis (which docker-compose runs) can never serve a semantic query, so
        # that call would only add startup latency to the first request.
        if not redisearch_available(redis_client):
            log.warning("semantic_cache_disabled", reason="redisearch_module_missing")
            return None

        from redisvl.utils.vectorize import OpenAITextVectorizer

        vectorizer = OpenAITextVectorizer(
            model=settings.EMBEDDING_MODEL,
            api_config={"api_key": settings.OPENAI_API_KEY},
        )
        return EstimationSemanticCache(
            redis_client=redis_client,
            vectorizer=vectorizer,
            threshold=settings.SEMANTIC_CACHE_THRESHOLD,
            ttl=settings.SEMANTIC_CACHE_TTL,
            log_only=settings.SEMANTIC_CACHE_LOG_ONLY,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "semantic_cache_disabled",
            reason="setup_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return None


@lru_cache
def get_estimation_service() -> EstimationService:
    settings = get_settings()
    return EstimationService(
        llm_wrapper=get_llm_wrapper(),
        exact_cache=get_cache(),
        semantic_cache=get_semantic_cache(),
        openai_client=get_openai_client(),
        metadata_extractor_model=settings.METADATA_EXTRACTOR_MODEL,
        compression_model=settings.COMPRESSION_MODEL,
        anchor_detection_mode=settings.ANCHOR_DETECTION_MODE,
        conversational_prompt_version=settings.CONVERSATIONAL_PROMPT_VERSION,
        critic_model=settings.CRITIC_MODEL,
        boss_max_iterations=settings.BOSS_MAX_ITERATIONS,
        enable_llm_metadata_fallback=settings.METADATA_LLM_FALLBACK,
    )


@lru_cache
def get_session_store() -> SessionStore:
    """In-memory store of conversational sessions. Singleton per worker.

    State lives in process memory: docker compose restart clears it, and
    workers > 1 each get their own copy. Documented limitation for the
    Session 5 exercise; persistence is module-3 territory.
    """
    settings = get_settings()
    return SessionStore(max_turns=settings.MAX_CONVERSATION_TURNS)