"""FastAPI dependency factories for shared singletons (cache, LLM wrapper, ledger)."""

from __future__ import annotations

from functools import lru_cache

from openai import OpenAI
from redis import from_url
from structlog import get_logger

from app.config import get_settings
from app.embedding_pipeline.chunking.base import Chunker
from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.chunking.strategies import (
    ContextualRetrievalChunker,
    FixedSizeChunker,
    HierarchicalChunker,
    PropositionalChunker,
    RecursiveChunker,
    SemanticChunker,
    SentenceWindowChunker,
)
from app.embedding_pipeline.embedder import MODEL as EMBEDDING_MODEL_NAME, OpenAIEmbedder
from app.embedding_pipeline.ingest_service import RagIngestService
from app.embedding_pipeline.retriever import SemanticRetriever
from app.embedding_pipeline.store.repository import ChunkStore
from app.persistence.database import get_async_session_factory, is_postgres_url
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
def get_chunker() -> JSONStructuralChunker:
    """Stateless structural chunker for the embedding pipeline (Session 7)."""
    return JSONStructuralChunker()


# --- Session 8 (Track B): one factory per chunking strategy -----------------
# The no-API strategies are plain singletons. The LLM-backed ones raise a clear
# RuntimeError when their key is missing; the comparison route maps that to a 500
# and an unknown name to a 400.


@lru_cache
def get_anthropic_client():
    """Lazy Anthropic client. ``None`` when no API key is configured."""
    settings = get_settings()
    if not settings.ANTHROPIC_API_KEY:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


@lru_cache
def get_fixed_size_chunker() -> FixedSizeChunker:
    return FixedSizeChunker()


@lru_cache
def get_recursive_chunker() -> RecursiveChunker:
    return RecursiveChunker()


@lru_cache
def get_sentence_window_chunker() -> SentenceWindowChunker:
    return SentenceWindowChunker()


@lru_cache
def get_hierarchical_chunker() -> HierarchicalChunker:
    return HierarchicalChunker()


@lru_cache
def get_semantic_chunker() -> SemanticChunker:
    settings = get_settings()
    # Raises RuntimeError with an actionable message when the key is missing.
    return SemanticChunker(api_key=settings.OPENAI_API_KEY, model=settings.EMBEDDING_MODEL)


# NOT @lru_cache: these are rebuilt per comparison request (construction is cheap;
# the underlying API clients stay singletons).
def get_propositional_chunker() -> PropositionalChunker:
    client = get_openai_client()
    if client is None:
        raise RuntimeError("PropositionalChunker requires OPENAI_API_KEY.")
    return PropositionalChunker(client=client)


def get_contextual_retrieval_chunker() -> ContextualRetrievalChunker:
    client = get_anthropic_client()
    if client is None:
        raise RuntimeError("ContextualRetrievalChunker requires ANTHROPIC_API_KEY.")
    return ContextualRetrievalChunker(client=client)


# Registry: strategy name -> factory. ``structural`` reuses ``get_chunker``.
# This order is the canonical comparison order used by "all".
CHUNKER_FACTORIES: dict[str, object] = {
    "structural": get_chunker,
    "fixed_size": get_fixed_size_chunker,
    "recursive": get_recursive_chunker,
    "sentence_window": get_sentence_window_chunker,
    "semantic": get_semantic_chunker,
    "propositional": get_propositional_chunker,
    "contextual_retrieval": get_contextual_retrieval_chunker,
    "hierarchical": get_hierarchical_chunker,
}

#: Canonical strategy order; ``"all"`` means exactly this list.
ALL_STRATEGIES: list[str] = list(CHUNKER_FACTORIES)


def build_chunkers(names: list[str]) -> list[Chunker]:
    """Instantiate the requested chunkers by name.

    Raises ``KeyError`` for an unknown strategy (mapped to 400) and
    ``RuntimeError`` for one whose API key is missing (mapped to 500).
    """
    chunkers: list[Chunker] = []
    for name in names:
        factory = CHUNKER_FACTORIES.get(name)
        if factory is None:
            raise KeyError(name)
        chunkers.append(factory())  # type: ignore[operator]
    return chunkers


@lru_cache
def get_embedder() -> OpenAIEmbedder | None:
    """OpenAI embedder for the embedding pipeline.

    Returns ``None`` when no ``OPENAI_API_KEY`` is configured; the router maps
    that to a generic 500. Mirrors ``get_semantic_cache``: the rest of the
    service stays usable on a DeepSeek-only deployment.
    """
    settings = get_settings()
    client = get_openai_client()
    if client is None:
        log.warning("embedder_disabled", reason="no_openai_key")
        return None
    return OpenAIEmbedder(client=client, model=settings.EMBEDDING_MODEL)


# --- Session 8: pgvector persistence + semantic search ---------------------


def vector_store_problem() -> str | None:
    """Why the vector store cannot be used right now, or ``None`` when it can.

    Three conditions make every stored distance meaningless or the schema
    unwritable, so they are refused up front instead of failing deep inside a
    request:

    * ``DATABASE_URL`` is not Postgres (the default is SQLite for the tests);
    * ``EMBEDDING_MODEL`` is not the 1536-dim model the column pins;
    * pgvector is not importable (a partial install).

    Returned as a reason string so the routers can log it while sending the
    client a generic 500.
    """
    settings = get_settings()
    if not is_postgres_url(settings.DATABASE_URL):
        return "database_not_postgres"
    if settings.EMBEDDING_MODEL != EMBEDDING_MODEL_NAME:
        return "embedding_model_dimension_mismatch"
    try:
        import pgvector.sqlalchemy  # noqa: F401
    except ImportError:  # pragma: no cover - a broken install, not a code path
        return "pgvector_missing"
    return None


@lru_cache
def get_chunk_store() -> ChunkStore:
    """Stateless async data-access layer over documents/chunks."""
    return ChunkStore()


@lru_cache
def get_rag_ingest_service() -> RagIngestService | None:
    """Chunk -> embed -> persist orchestration.

    ``None`` when the service cannot work at all: no OpenAI key (embeddings),
    a non-Postgres database (pgvector schema), or a mismatched embedding model.
    The router maps that to a generic 500 and the reason to the log.
    """
    embedder = get_embedder()
    if embedder is None:
        return None
    problem = vector_store_problem()
    if problem is not None:
        log.warning("rag_ingest_service_disabled", reason=problem)
        return None
    return RagIngestService(
        chunker=get_chunker(),
        embedder=embedder,
        session_factory=get_async_session_factory(),
        store=get_chunk_store(),
    )


@lru_cache
def get_semantic_retriever() -> SemanticRetriever | None:
    """Query-side counterpart of the ingest service. Same ``None`` contract."""
    embedder = get_embedder()
    if embedder is None:
        return None
    problem = vector_store_problem()
    if problem is not None:
        log.warning("semantic_retriever_disabled", reason=problem)
        return None
    return SemanticRetriever(
        embedder=embedder,
        session_factory=get_async_session_factory(),
        store=get_chunk_store(),
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