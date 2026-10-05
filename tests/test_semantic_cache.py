"""Unit tests for the semantic cache's pure and degradation behaviour.

No Redis, no embeddings: the vectorizer is an inert object and the Redis client
is a stand-in that only answers ``module_list()``. What is pinned here is the
bucket key and the guarantee that a Redis without RediSearch turns the cache
into a no-op instead of failing the request.
"""

import redis

from app.schemas.new_estimation_schema import (
    DetailLevel,
    EstimationRequest,
    EstimationResult,
    OutputFormat,
    Phase,
    ProjectType,
)
from app.services.semantic_cache import EstimationSemanticCache, redisearch_available


class FakeRedis(redis.Redis):
    """A real ``redis.Redis`` subclass that never connects.

    ``redisvl`` validates that the client is a ``Redis`` instance, so a plain
    stand-in is rejected; only ``module_list()`` is consulted before the first
    query, so overriding it is enough.
    """

    def __init__(self, modules) -> None:
        super().__init__()
        self._modules = modules

    def module_list(self) -> list:
        return self._modules


def request(**overrides) -> EstimationRequest:
    base = {
        "description": "A mobile marketplace for second-hand furniture.",
        "project_type": ProjectType.MOBILE_APP,
        "detail_level": DetailLevel.MEDIUM,
        "output_format": OutputFormat.PHASES_TABLE,
    }
    return EstimationRequest(**{**base, **overrides})


def result() -> EstimationResult:
    return EstimationResult(
        summary="A short but sufficiently long overall summary.",
        confidence_pct=80,
        phases=[
            Phase(
                name="Discovery",
                duration_weeks=1,
                cost_eur=500,
                summary="Discovery workshops and scope definition.",
            )
        ],
        total_duration_weeks=1,
        total_cost_eur=500,
    )


# --- the bucket is the exact-match guard ------------------------------------


def test_bucket_composes_version_and_every_form_option():
    bucket = EstimationSemanticCache.bucket_for(request(), "v1")

    assert bucket == "v1:mobile_app:medium:phases_table"


def test_two_form_options_never_share_a_bucket():
    a = EstimationSemanticCache.bucket_for(request(), "v1")
    b = EstimationSemanticCache.bucket_for(request(detail_level=DetailLevel.DETAILED), "v1")

    assert a != b


# --- degradation on a Redis without RediSearch ------------------------------


def test_a_redis_without_the_search_module_disables_the_cache():
    cache = EstimationSemanticCache(
        redis_client=FakeRedis([{"name": b"ReJSON"}]), vectorizer=object()
    )

    assert cache.available is False
    assert cache.lookup(request(), "v1") is None
    cache.store(request(), result(), "v1")  # no-op, must not raise


def test_the_search_module_is_detected_when_present():
    """The detection helper is the cheap guard; it must not need a live Redis."""
    cache = EstimationSemanticCache.__new__(EstimationSemanticCache)
    cache.redis_client = FakeRedis([{"name": b"search"}])

    assert cache._has_search_module() is True


def test_the_search_module_is_not_detected_when_absent():
    cache = EstimationSemanticCache.__new__(EstimationSemanticCache)
    cache.redis_client = FakeRedis([{"name": b"ReJSON"}])

    assert cache._has_search_module() is False


def test_an_unreachable_redis_disables_the_cache_instead_of_raising():
    class UnreachableRedis:
        def module_list(self):
            raise ConnectionError("connection refused")

    cache = EstimationSemanticCache(redis_client=UnreachableRedis(), vectorizer=object())

    assert cache.available is False
    assert cache.lookup(request(), "v1") is None


def test_redisearch_available_is_a_plain_function_over_a_client():
    assert redisearch_available(FakeRedis([{"name": b"search"}])) is True
    assert redisearch_available(FakeRedis([{"name": b"ReJSON"}])) is False


def test_the_dependency_skips_the_embedding_probe_without_redisearch(monkeypatch):
    """Regression: the embedding client probes OpenAI at construction, so the
    RediSearch check must come first or the first request blocks on the network
    even though a vanilla Redis can never serve the cache."""
    from app import dependencies
    from tests.factories import build_settings

    settings = build_settings(OPENAI_API_KEY="test-openai-key")
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(
        dependencies, "from_url", lambda *args, **kwargs: FakeRedis([{"name": b"ReJSON"}])
    )
    monkeypatch.setattr(dependencies, "redisearch_available", lambda client: False)

    class ExplodingVectorizer:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the embedding client must not be constructed")

    monkeypatch.setattr("redisvl.utils.vectorize.OpenAITextVectorizer", ExplodingVectorizer)

    dependencies.get_semantic_cache.cache_clear()
    dependencies.get_openai_client.cache_clear()
    try:
        assert dependencies.get_semantic_cache() is None
    finally:
        dependencies.get_semantic_cache.cache_clear()
        dependencies.get_openai_client.cache_clear()
