"""Boundary tests for the retired ``POST /search`` route (Session 10).

Spec 016 replaced ``/search`` with ``POST /v1/retrieval/search`` and deleted
``app/embedding_pipeline/search_router.py``. What is left to assert here is the
retirement itself: the old path is a 404 and is absent from the published schema,
and the replacement is registered with the request and response models it
promises. Behavioural coverage of the replacement — the four configurations, the
422 bounds, the 502/500 failure bodies — lives in ``tests/test_retrieval_route.py``
and is deliberately **not** repeated here. That file already pins the 200
``RetrievalResult`` schema, so the one duplicate this rewrite would otherwise have
introduced was dropped; what remains is the part that file does not check.

Hermetic: ``TestClient`` against the application, no database, no provider, no
model weights.
"""

from __future__ import annotations

from app.embedding_pipeline.schemas import MAX_QUERY_CHARS, MAX_SEARCH_K
from app.generation.rag.retrieval.schemas import MAX_RECALL_K

RETIRED_URL = "/search"
URL = "/v1/retrieval/search"

#: Bounds the route's ``Field(...)`` declarations publish, restated here so a
#: silent loosening of the contract fails this file rather than only a reviewer.
MAX_RRF_K = 200
MAX_DISTANCE_THRESHOLD = 2.0


def openapi_spec(client) -> dict:
    return client.get("/openapi.json").json()


def resolve(prop: dict, spec: dict) -> dict:
    """Follow a local ``$ref`` one level, so bounds are readable either way."""
    reference = prop.get("$ref")
    if not reference:
        return prop
    return spec["components"]["schemas"][reference.rsplit("/", 1)[-1]]


def real_type(prop: dict, spec: dict) -> dict:
    """The non-``null`` branch of a property that pydantic rendered as ``anyOf``.

    ``int | None`` becomes ``{"anyOf": [{"type": "integer", ...}, {"type":
    "null"}]}``, so a bound or an enum can sit one level below the property.
    """
    for branch in prop.get("anyOf", [prop]):
        if branch.get("type") != "null":
            return resolve(branch, spec)
    return resolve(prop, spec)


def numeric_bounds(prop: dict, spec: dict) -> tuple[float | None, float | None]:
    branch = real_type(prop, spec)
    return branch.get("minimum"), branch.get("maximum")


# --- the retirement ---------------------------------------------------------


def test_the_retired_search_route_returns_404(client) -> None:
    """The Session-8 body is still a valid-looking request; the route is gone."""
    response = client.post(RETIRED_URL, json={"query": "OAuth for fintech", "k": 5})

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_the_retired_search_route_is_absent_from_openapi(client) -> None:
    assert RETIRED_URL not in openapi_spec(client)["paths"]


# --- the replacement -------------------------------------------------------


def test_the_replacement_route_is_registered_with_its_models(client) -> None:
    spec = openapi_spec(client)

    assert URL in spec["paths"]
    operation = spec["paths"][URL]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert body.endswith("RetrievalRequest")
    assert response.endswith("RetrievalResult")
    # Bad bounds are documented as a 422, not as a 500.
    assert "422" in operation["responses"]


def test_the_published_request_schema_keeps_the_contract_bounds(client) -> None:
    """The bounds a client can rely on, read from the published schema."""
    spec = openapi_spec(client)
    properties = spec["components"]["schemas"]["RetrievalRequest"]["properties"]

    assert properties["query"]["minLength"] == 1
    assert properties["query"]["maxLength"] == MAX_QUERY_CHARS == 20_000
    assert properties["top_k"]["maximum"] == MAX_SEARCH_K == 50
    assert properties["top_k"]["default"] == 10
    assert properties["recall_k"]["maximum"] == MAX_RECALL_K == 200
    assert properties["recall_k"]["default"] == 50
    assert numeric_bounds(properties["rrf_k"], spec) == (1, MAX_RRF_K)
    assert numeric_bounds(properties["distance_threshold"], spec) == (
        0.0,
        MAX_DISTANCE_THRESHOLD,
    )
    # A closed literal, not a free string: an unknown mode is a 422 client bug.
    assert real_type(properties["search_mode"], spec)["enum"] == ["vector", "hybrid"]


def test_the_replacement_route_requires_no_authentication(client) -> None:
    """No security on the replacement, consistent with the rest of the repository.

    Spec 016 records the decision: adding an auth scheme to one route only would
    make this service internally inconsistent.
    """
    assert "security" not in openapi_spec(client)["paths"][URL]["post"]
