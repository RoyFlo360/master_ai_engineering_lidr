"""HTTP tests for ``POST /v1/estimate/from-transcript`` and the stage routes (T0B).

House style follows ``tests/test_retrieval_route.py``: the transport is real, the
billed collaborators are fakes. Rather than replacing the orchestrator with one
mock, the **stages** are patched (reformulate → retrieve → assemble → generate)
so the real orchestration, the real assembler and the real idempotency store run
underneath the route. That is what makes ``cached=True`` a property of the
implementation instead of a property of the test's own dictionary.

What this file pins:

* 200 with the documented ``EstimateFromTranscriptResponse`` shape for a
  grounded answer, and 200 with ``grounded=False`` when retrieval returns
  nothing — with the real ``assemble``/``generate_estimate`` deciding that from
  an empty chunk list;
* 422 for a transcript below ``MIN_TRANSCRIPT_CHARS`` and for an unknown body
  field, before any stage runs;
* 502 with the ``GenerationError`` public message and no leaked detail;
* ``cached=True`` on the second identical request with the same
  ``idempotency_key``, and the generator called only once;
* the four ``/stages/*`` routes answer with ``StageEnvelope`` plus their stage
  payload;
* no body carries a stack trace or credential material.

Two things the contract does not pin are discovered at runtime and reported as
gaps rather than assumed: the router's module path (``estimate_router`` is the
name in the brief) and, partly, the ``/stages/*`` request bodies. The module
resolver names every path it tried in its failure message.
"""

from __future__ import annotations

import importlib

import pytest

from app.generation.rag.errors import GenerationError
from app.generation.rag.retrieval.schemas import RetrievalResult, RetrievedChunk
from app.generation.rag.schemas import (
    Citation,
    EstimatedModule,
    EstimatedTask,
    EstimateFromTranscriptResponse,
    EstimationQuery,
    GroundedEstimate,
)

URL = "/v1/estimate/from-transcript"
STAGE_PREFIX = "/v1/estimate/stages"

#: The brief calls the module ``estimate_router``; accept the neighbours so the
#: test reports a 404/422 problem rather than a path problem.
ROUTER_MODULES = (
    "app.generation.rag.estimate_router",
    "app.generation.rag.router",
    "app.routers.estimate",
)

#: A credential-shaped sentinel; never written as a provider key literal.
SECRET = "credential-material-alpha"

TRANSCRIPT = (
    "Discovery workshop with the client's operations team. We agreed to build an "
    "OAuth 2.0 authentication backend with PKCE, refresh-token rotation and RBAC "
    "for three roles. The client is a mid-size insurer; the stack is Python and "
    "PostgreSQL, delivery during 2024, with a budget comparable to last year's "
    "identity programme."
)

RESPONSE_KEYS = {
    "estimate",
    "query",
    "chunks",
    "retrieved",
    "cached",
    "validation_retries",
    "pipeline_ms",
}

_UNSET = object()
#: Sentinel for ``install_stages``: leave this stage as the real implementation.
KEEP = object()


# --- samples ----------------------------------------------------------------


def sample_query() -> EstimationQuery:
    return EstimationQuery(
        summary="OAuth 2.0 authentication backend with RBAC.",
        search_query="oauth authentication backend rbac insurance 2024",
        sector="insurance",
        technologies=["python", "postgresql"],
        project_year=2024,
        confidence=0.82,
        source="llm",
    )


def sample_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1,
        document_id=10,
        budget_id="BUD-2024-001",
        component_id="CMP-001",
        chunk_type="budget_component",
        content="The authentication module took 120 hours.",
        sector="finance",
        project_year=2024,
        distance=0.12,
        source="vector",
    )


def sample_estimate(
    *, chunk_ids: list[int] | None = None, grounded: bool = True
) -> GroundedEstimate:
    ids = [1] if chunk_ids is None else chunk_ids
    return GroundedEstimate(
        summary="Authentication backend, 160 hours.",
        modules=[
            EstimatedModule(
                name="Authentication",
                tasks=[EstimatedTask(name="OAuth 2.0 flow", estimated_hours=160)],
                estimated_hours=160,
            )
        ],
        total_hours=160,
        citations=[
            Citation(
                chunk_id=chunk_id,
                budget_id="BUD-2024-001",
                claim="Supports the authentication estimate.",
            )
            for chunk_id in ids
        ],
        confidence_pct=74 if grounded else 0,
        grounded=grounded,
    )


def sample_result(chunk_count: int = 1) -> RetrievalResult:
    chunks = [sample_chunk()] if chunk_count else []
    return RetrievalResult(
        chunks=chunks,
        search_mode="vector",
        rerank=False,
        candidates_evaluated=len(chunks),
        search_time_ms=3,
    )


# --- the seam ---------------------------------------------------------------


def estimate_router_module():
    """The module that owns the estimate endpoints, by any conventional name."""
    tried: list[str] = []
    for name in ROUTER_MODULES:
        tried.append(name)
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    pytest.fail("no estimate router module exists yet; tried: " + ", ".join(tried))


def _module_or_none(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return None


def hermetic_settings():
    from tests.factories import build_settings

    return build_settings()


def patch_settings_seams(monkeypatch, module) -> None:
    """Point any settings accessor in ``module`` at hermetic settings.

    ``tests/conftest.py`` only rewires ``llm_service`` and ``main``, so a new
    chain module that calls ``get_settings`` would otherwise read the developer's
    ``.env`` — exactly what ``rules/testing.md`` forbids.
    """
    for name in ("get_settings", "get_estimate_settings", "get_estimation_settings"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, hermetic_settings)


class Recorder:
    """Shared call log for the stage fakes."""

    def __init__(self) -> None:
        self.reformulate: list[dict] = []
        self.retrieve: list[dict] = []
        self.generate: list[dict] = []


def install_stages(
    monkeypatch,
    *,
    reformulate=_UNSET,
    retrieve=_UNSET,
    generate=_UNSET,
) -> Recorder:
    """Patch the stage seams in every module that could hold them.

    ``estimate_router`` may import ``reformulate``/``retrieve``/``generate_estimate``
    directly, and the orchestrator may do the same, so each replacement is
    installed wherever the name appears. At least one seam per patched stage must
    exist, otherwise the test says so instead of passing vacuously.
    """
    recorder = Recorder()
    router = estimate_router_module()
    orchestrator = _module_or_none("app.generation.rag.orchestrator")
    modules = [module for module in (router, orchestrator) if module is not None]
    for module in modules:
        patch_settings_seams(monkeypatch, module)

    if reformulate is _UNSET:
        async def reformulate(transcript, *args, **kwargs):
            recorder.reformulate.append({"transcript": transcript, **kwargs})
            return sample_query()

    if retrieve is _UNSET:
        async def retrieve(*args, **kwargs):
            recorder.retrieve.append(kwargs)
            return sample_result(1)

    if generate is _UNSET:
        async def generate(*args, **kwargs):
            recorder.generate.append(kwargs)
            return sample_estimate()

    installed = 0
    for module in modules:
        for name, replacement in (
            ("reformulate", reformulate),
            ("retrieve", retrieve),
            ("generate_estimate", generate),
        ):
            if replacement is KEEP:
                continue
            if hasattr(module, name):
                monkeypatch.setattr(module, name, replacement)
                installed += 1

    stage_modules = (
        ("app.generation.rag.query_reformulator", "reformulate", reformulate),
        ("app.generation.rag.retrieval.pipeline", "retrieve", retrieve),
        ("app.generation.rag.estimator", "generate_estimate", generate),
    )
    for module_name, name, replacement in stage_modules:
        if replacement is KEEP:
            continue
        module = _module_or_none(module_name)
        if module is not None and hasattr(module, name):
            monkeypatch.setattr(module, name, replacement)
            installed += 1

    assert installed, "the estimate chain exposes no injectable stage seam"
    return recorder


# --- stage-route fakes ------------------------------------------------------


async def fake_reformulate(*args, **kwargs):
    return sample_query()


async def fake_retrieve(*args, **kwargs):
    return sample_result(1)


def fake_assemble(*args, **kwargs) -> str:
    return (
        "<source id=1 budget=BUD-2024-001 sector=finance year=2024>\n"
        "The authentication module took 120 hours."
    )


async def fake_generate(*args, **kwargs):
    return sample_estimate()


STAGE_SEAMS = {
    "reformulate": (
        "app.generation.rag.query_reformulator",
        {"reformulate": fake_reformulate},
    ),
    "retrieve": ("app.generation.rag.retrieval.pipeline", {"retrieve": fake_retrieve}),
    "assemble": ("app.generation.rag.context_assembler", {"assemble": fake_assemble}),
    "generate": (
        "app.generation.rag.estimator",
        {"generate_estimate": fake_generate},
    ),
}


def install_stage(monkeypatch, stage: str) -> None:
    router = estimate_router_module()
    module_name, replacements = STAGE_SEAMS[stage]
    stage_module = _module_or_none(module_name)
    holders = [router] + ([stage_module] if stage_module is not None else [])
    for module in holders:
        patch_settings_seams(monkeypatch, module)

    installed = 0
    for module in holders:
        for name, replacement in replacements.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, replacement)
                installed += 1
    assert installed, f"stage {stage!r} exposes no injectable seam in {router.__name__}"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def idempotency():
    """The real store, emptied before and after each test."""
    module = importlib.import_module("app.generation.rag.idempotency")
    store = module.get_idempotency_store()
    store.clear()
    try:
        yield store
    finally:
        store.clear()


@pytest.fixture
def chain(client, monkeypatch, idempotency, settings_holder):
    """Resolve the router up front and rewire the app's settings for one test.

    ``settings_holder`` only reaches ``llm_service`` and ``main`` (``conftest.py``),
    so two extra seams are needed for hermeticity: any module-level ``get_settings``
    in the chain is patched by ``install_stages``, and the router's
    ``Depends(get_estimate_settings)`` is overridden here — FastAPI bound the
    dependency at decoration time, so patching the module attribute would not be
    consulted and the real ``.env`` would be read.
    """

    def _install(**overrides) -> Recorder:
        import app.main as main

        router = estimate_router_module()
        main.app.dependency_overrides.clear()
        settings_holder.current = hermetic_settings()
        for name in ("get_estimate_settings",):
            dependency = getattr(router, name, None)
            if dependency is not None:
                main.app.dependency_overrides[dependency] = hermetic_settings
        return install_stages(monkeypatch, **overrides)

    yield _install
    main = importlib.import_module("app.main")
    main.app.dependency_overrides.clear()


def post(client, payload: dict):
    return client.post(URL, json=payload)


def _chunk_id(chunk) -> int:
    """``RetrievedChunk`` or the dict a loosely-typed ``list`` field keeps."""
    if isinstance(chunk, dict):
        return chunk["chunk_id"]
    return chunk.chunk_id


# --- success shapes ---------------------------------------------------------


def test_a_grounded_response_has_the_documented_shape(chain, client) -> None:
    recorder = chain()

    response = post(client, {"transcript": TRANSCRIPT, "top_k": 5})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["cached"] is False
    assert body["retrieved"] == len(body["chunks"]) == 1
    assert body["validation_retries"] in (0, 1)
    assert body["pipeline_ms"] >= 0

    estimate = body["estimate"]
    assert set(estimate) == set(GroundedEstimate.model_fields)
    assert estimate["grounded"] is True
    assert [citation["chunk_id"] for citation in estimate["citations"]] == [1]
    assert estimate["confidence_pct"] == 74

    assert set(body["query"]) == set(EstimationQuery.model_fields)
    assert set(body["chunks"][0]) == set(RetrievedChunk.model_fields)
    # The body validates against the declared response model, not just its keys.
    parsed = EstimateFromTranscriptResponse.model_validate(body)
    assert parsed.retrieved == 1
    assert parsed.estimate.grounded is True
    # ``chunks`` is declared ``list`` (loose, to avoid a circular import), so a
    # validated body keeps the chunks as dicts — assert on the ids either way.
    assert [_chunk_id(chunk) for chunk in parsed.chunks] == [1]
    assert recorder.reformulate, "the reformulator was never called"
    assert recorder.retrieve, "retrieval was never called"
    assert recorder.generate, "generation was never called"


def test_an_empty_retrieval_is_a_200_with_grounded_false(chain, client) -> None:
    """No chunks -> the *real* assembler and generator decide it is ungrounded."""

    async def empty_retrieval(*args, **kwargs):
        return sample_result(0)

    recorder = chain(retrieve=empty_retrieval, generate=KEEP)

    response = post(client, {"transcript": TRANSCRIPT})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retrieved"] == 0
    assert body["chunks"] == []
    assert body["estimate"]["grounded"] is False
    assert body["estimate"]["citations"] == []
    assert body["estimate"]["confidence_pct"] == 0
    # Nothing was retrieved, so nothing was generated.
    assert recorder.generate == []


# --- 422, before any stage --------------------------------------------------


def test_a_transcript_below_the_minimum_is_a_422(chain, client) -> None:
    recorder = chain()

    response = post(client, {"transcript": "x" * 99})

    assert response.status_code == 422, response.text
    assert recorder.reformulate == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"transcript": TRANSCRIPT, "unexpected": 1}, id="unknown-field"),
        pytest.param({"transcript": ""}, id="empty-transcript"),
        pytest.param({}, id="missing-transcript"),
    ],
)
def test_invalid_bodies_are_rejected_before_the_chain_runs(
    chain, client, payload: dict
) -> None:
    recorder = chain()

    response = post(client, payload)

    assert response.status_code == 422, response.text
    assert recorder.reformulate == []


# --- 502 --------------------------------------------------------------------


def test_a_generation_failure_is_a_502_without_leaking_detail(chain, client) -> None:
    async def exploding_generate(*args, **kwargs):
        raise GenerationError(f"provider failed, api_key={SECRET}")

    chain(generate=exploding_generate)

    response = post(client, {"transcript": TRANSCRIPT})

    assert response.status_code == 502, response.text
    assert response.json() == {"detail": GenerationError.public_message}
    assert SECRET not in response.text
    assert "Traceback" not in response.text


def test_a_success_body_carries_no_traceback_or_key(chain, client) -> None:
    chain()

    response = post(client, {"transcript": TRANSCRIPT})

    assert response.status_code == 200, response.text
    assert "Traceback" not in response.text
    assert SECRET not in response.text


# --- idempotency ------------------------------------------------------------


def test_the_second_identical_request_is_cached(chain, client) -> None:
    recorder = chain()
    payload = {"transcript": TRANSCRIPT, "idempotency_key": "replay-001"}

    first = post(client, payload)
    second = post(client, payload)

    assert first.status_code == 200, first.text
    assert first.json()["cached"] is False
    assert second.status_code == 200, second.text
    assert second.json()["cached"] is True
    assert second.json()["estimate"] == first.json()["estimate"]
    # A replay must not pay for a second generation.
    assert len(recorder.generate) == 1


def test_a_different_idempotency_key_is_not_a_replay(chain, client) -> None:
    chain()

    first = post(client, {"transcript": TRANSCRIPT, "idempotency_key": "replay-001"})
    second = post(client, {"transcript": TRANSCRIPT, "idempotency_key": "replay-002"})

    assert first.json()["cached"] is False
    assert second.json()["cached"] is False


# --- the four stage routes --------------------------------------------------
#
# Every stage answers with ``StageEnvelope`` (``stage`` + ``latency_ms``) plus
# its own payload key, per the contract.

STAGES = [
    pytest.param(
        "reformulate",
        f"{STAGE_PREFIX}/reformulate",
        {"transcript": TRANSCRIPT},
        {"stage", "latency_ms", "query"},
        "query",
        id="reformulate",
    ),
    pytest.param(
        "retrieve",
        f"{STAGE_PREFIX}/retrieve",
        {"query": "oauth authentication backend"},
        {"stage", "latency_ms", "result", "chunks"},
        "result",
        id="retrieve",
    ),
    pytest.param(
        "assemble",
        f"{STAGE_PREFIX}/assemble",
        {"chunks": [sample_chunk().model_dump()]},
        {"stage", "latency_ms", "context", "sources"},
        "context",
        id="assemble",
    ),
    pytest.param(
        "generate",
        f"{STAGE_PREFIX}/generate",
        {
            "query": sample_query().model_dump(),
            "context": "<source id=1 budget=BUD-2024-001 sector=finance year=2024>\nx",
            "source_ids": [1],
        },
        {"stage", "latency_ms", "estimate"},
        "estimate",
        id="generate",
    ),
]


@pytest.mark.parametrize("stage, path, payload, expected_keys, probe", STAGES)
def test_each_stage_route_answers_with_its_documented_shape(
    chain,
    client,
    monkeypatch,
    stage: str,
    path: str,
    payload: dict,
    expected_keys: set[str],
    probe: str,
) -> None:
    chain()
    install_stage(monkeypatch, stage)

    response = client.post(path, json=payload)

    assert response.status_code == 200, (path, response.text)
    body = response.json()
    assert set(body) == expected_keys, (path, sorted(body))
    assert body["stage"] == stage
    assert body["latency_ms"] >= 0
    assert probe in body, body


def test_the_reformulate_stage_returns_the_query_object(
    chain, client, monkeypatch
) -> None:
    chain()
    install_stage(monkeypatch, "reformulate")

    response = client.post(
        f"{STAGE_PREFIX}/reformulate", json={"transcript": TRANSCRIPT}
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["query"]) == set(EstimationQuery.model_fields)


def test_the_assemble_stage_route_reports_the_ids_it_emitted(
    chain, client, monkeypatch
) -> None:
    chain()
    install_stage(monkeypatch, "assemble")

    response = client.post(
        f"{STAGE_PREFIX}/assemble", json={"chunks": [sample_chunk().model_dump()]}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sources"] == [1]
    assert "<source" in body["context"]


def test_the_stage_routes_restrict_their_bodies(chain, client, monkeypatch) -> None:
    """An unknown field is a 422 on the stage surface too, not a silent ignore."""
    chain()
    install_stage(monkeypatch, "reformulate")

    response = client.post(
        f"{STAGE_PREFIX}/reformulate",
        json={"transcript": TRANSCRIPT, "unexpected": 1},
    )

    assert response.status_code == 422, response.text


# --- contract visibility ----------------------------------------------------


def test_the_route_is_documented_in_openapi(client) -> None:
    spec = client.get("/openapi.json").json()

    assert URL in spec["paths"]
    operation = spec["paths"][URL]["post"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema.get("$ref", "").endswith("EstimateFromTranscriptResponse")
