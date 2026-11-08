"""Security tests for the retrieval surface (Session 10, T5.4).

The route is unauthenticated and every request bills an embedding call, so the
tests pin: the request's caps and closed sets (rejected with 422 *before* the
pipeline runs), unknown-field rejection, the rule that a failure body carries no
query text / chunk text / key material, and that the reranker model can only
come from ``Settings``.

Hermetic: the router-level cases inject a fake pipeline; neither the embedder
nor the store is real.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from pydantic import ValidationError

from app.generation.rag.retrieval.errors import RetrievalUnavailable
from app.generation.rag.retrieval.schemas import (
    RetrievalRequest,
    RetrievalResult,
    RetrievedChunk,
)

ROUTE = "/v1/retrieval/search"

# Distinct sentinels: the success case may contain the chunk text (that is the
# payload) but must never echo the query or credential material, and the 500
# body must contain none of the three.
SECRET_QUERY = "confidential query payload alpha"
SECRET_CHUNK = "private chunk body beta"
SECRET_KEY = "credential material gamma"

VALID_REQUEST = {"query": "mobile banking API with OAuth 2.0"}


def request(**overrides: Any) -> dict[str, Any]:
    return {**VALID_REQUEST, **overrides}


# --- request bounds (422 before any billed work) ---------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(request(query="x" * 20_001), id="query-too-long"),
        pytest.param(request(query=""), id="query-empty"),
        pytest.param(request(top_k=0), id="top_k-zero"),
        pytest.param(request(top_k=51), id="top_k-above-cap"),
        pytest.param(request(recall_k=0), id="recall_k-zero"),
        pytest.param(request(recall_k=201), id="recall_k-above-cap"),
        pytest.param(request(rrf_k=0), id="rrf_k-zero"),
        pytest.param(request(rrf_k=201), id="rrf_k-above-cap"),
        pytest.param(request(distance_threshold=-0.1), id="threshold-negative"),
        pytest.param(request(distance_threshold=2.5), id="threshold-above-cap"),
        pytest.param(request(search_mode="semantic"), id="search-mode-not-literal"),
        pytest.param(request(search_mode="VECTOR"), id="search-mode-case-sensitive"),
        pytest.param(request(reranker_model="evil/weights"), id="unknown-reranker-field"),
        pytest.param(request(model="evil/weights"), id="unknown-model-field"),
        pytest.param(request(extra="anything"), id="unknown-field"),
    ],
)
def test_retrieval_request_rejects_out_of_bounds_input(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RetrievalRequest.model_validate(payload)


def test_the_documented_boundaries_are_accepted() -> None:
    """The caps must reject what is outside them, not what sits on the edge."""
    parsed = RetrievalRequest.model_validate(
        request(
            query="x" * 20_000,
            top_k=50,
            recall_k=200,
            rrf_k=200,
            distance_threshold=2.0,
            search_mode="hybrid",
            rerank=True,
        )
    )

    assert len(parsed.query) == 20_000
    assert parsed.distance_threshold == 2.0


def _enum_values(node: Any, defs: dict[str, Any] | None = None) -> set[str]:
    defs = defs or {}
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            name = node["$ref"].rsplit("/", 1)[-1]
            return _enum_values(defs.get(name, {}), defs)
        values: set[str] = set()
        if isinstance(node.get("enum"), list):
            values.update(value for value in node["enum"] if isinstance(value, str))
        for child in node.values():
            values.update(_enum_values(child, defs))
        return values
    if isinstance(node, list):
        values = set()
        for child in node:
            values.update(_enum_values(child, defs))
        return values
    return set()


def test_search_mode_is_a_closed_set() -> None:
    schema = RetrievalRequest.model_json_schema()
    values = _enum_values(
        schema["properties"]["search_mode"], schema.get("$defs", {})
    )
    assert values == {"vector", "hybrid"}


def test_the_reranker_model_never_comes_from_the_request() -> None:
    forbidden = {"model", "model_name", "reranker_model", "cross_encoder"}
    assert not (forbidden & set(RetrievalRequest.model_fields))

    for field in forbidden:
        with pytest.raises(ValidationError):
            RetrievalRequest.model_validate(request(**{field: "attacker/weights"}))


# --- router-level: injection and disclosure --------------------------------


@pytest.fixture
def retrieval_dependencies():
    """Replace the router's dependencies so no real embedder/store is built."""
    import app.main as main
    from app.config import Settings
    from app.dependencies import get_chunk_store, get_embedder, get_reranker

    router_module = importlib.import_module("app.generation.rag.retrieval.router")
    overrides = {
        get_embedder: lambda: object(),
        get_chunk_store: lambda: object(),
        get_reranker: lambda: None,
        router_module.get_retrieval_settings: lambda: Settings(
            _env_file=None, DEEPSEEK_API_KEY="x"
        ),
        router_module.get_retrieval_session_factory: lambda: object(),
    }

    main.app.dependency_overrides.update(overrides)
    try:
        yield
    finally:
        for dependency in overrides:
            main.app.dependency_overrides.pop(dependency, None)


def install_fake_pipeline(monkeypatch, fake) -> None:
    """Patch the router's pipeline seam, whatever import style it uses."""
    router_module = importlib.import_module("app.generation.rag.retrieval.router")

    seams = 0
    if hasattr(router_module, "retrieve"):
        monkeypatch.setattr(router_module, "retrieve", fake)
        seams += 1

    pipeline_module = getattr(router_module, "pipeline", None)
    if pipeline_module is not None and hasattr(pipeline_module, "retrieve"):
        monkeypatch.setattr(pipeline_module, "retrieve", fake)
        seams += 1

    assert seams, "the router exposes no injectable pipeline seam"


def sample_result() -> RetrievalResult:
    return RetrievalResult(
        chunks=[
            RetrievedChunk(
                chunk_id=1,
                document_id=2,
                budget_id="BUD-2024-001",
                component_id="C-1",
                chunk_type="budget_component",
                content=SECRET_CHUNK,
                distance=0.1,
                source="vector",
            )
        ],
        search_mode="vector",
        rerank=False,
        candidates_evaluated=1,
        search_time_ms=1,
        low_confidence=False,
    )


def test_success_response_does_not_echo_the_query_or_embeddings(
    client, monkeypatch, retrieval_dependencies
) -> None:
    async def fake_pipeline(**_kwargs):
        return sample_result()

    install_fake_pipeline(monkeypatch, fake_pipeline)

    response = client.post(ROUTE, json=request(query=SECRET_QUERY))

    assert response.status_code == 200, response.text
    body = response.text
    assert SECRET_QUERY not in body
    assert SECRET_KEY not in body
    # No vector leaks into the wire contract; content is the only payload text.
    assert "query_vector" not in body
    assert SECRET_CHUNK in body


def test_error_body_on_a_500_carries_no_query_chunk_or_key_material(
    client, monkeypatch, retrieval_dependencies
) -> None:
    async def failing_pipeline(**_kwargs):
        raise RetrievalUnavailable(f"{SECRET_QUERY} {SECRET_CHUNK} {SECRET_KEY}")

    install_fake_pipeline(monkeypatch, failing_pipeline)

    response = client.post(ROUTE, json=request(query=SECRET_QUERY))

    assert response.status_code == 500, response.text
    body = response.text
    for secret in (SECRET_QUERY, SECRET_CHUNK, SECRET_KEY):
        assert secret not in body
    assert "query_vector" not in body
    assert "Traceback" not in body
    assert response.json().get("detail")


def test_invalid_bounds_are_rejected_before_the_pipeline_runs(
    client, monkeypatch, retrieval_dependencies
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return sample_result()

    install_fake_pipeline(monkeypatch, fake_pipeline)

    for payload in (request(top_k=51), request(query="x" * 20_001), request(rrf_k=0)):
        response = client.post(ROUTE, json=payload)
        assert response.status_code == 422, (payload, response.text)

    assert calls == [], "validation must reject before the pipeline (and its embedding) runs"
