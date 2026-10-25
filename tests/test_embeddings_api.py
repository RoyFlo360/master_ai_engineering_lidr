"""HTTP tests for the persisting ``POST /embeddings/ingest`` (Session 8).

We bypass Postgres and OpenAI entirely: ``get_rag_ingest_service`` is overridden
with an in-memory fake that mimics the service contract (response model +
``DuplicateDocumentError``). This exercises the router wiring, the literal 409
shape and the error mapping without infrastructure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.dependencies import get_rag_ingest_service
from app.embedding_pipeline.ingest_service import (
    DuplicateDocumentError,
    EmbeddingDimensionMismatch,
)
from app.embedding_pipeline.schemas import IngestResponse

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"
URL = "/embeddings/ingest"


def budget_payload(budget_id: str = "BUD-2024-001", component_id: str = "AUTH-001") -> dict:
    return {
        "budget_id": budget_id,
        "client_metadata": {"name": "FintechCorp", "sector": "finance", "country": "ES"},
        "project_summary": "Mobile banking API with OAuth 2.0",
        "main_technology": "ruby_on_rails",
        "year": 2024,
        "total_estimated_hours": 120,
        "components": [
            {
                "component_id": component_id,
                "name": "OAuth 2.0 backend",
                "description": "OAuth flows with JWT session management.",
                "tech_stack": ["ruby_on_rails", "postgresql"],
                "estimated_hours": 120,
                "complexity": "high",
                "dependencies": [],
            }
        ],
    }


def ingest_payload() -> dict:
    return {
        "source_path": "data/budgets_sample.json::BUD-2024-001",
        "document_type": "historical_budget",
        "content": budget_payload(),
    }


def first_sample_budget() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[0]


class FakeRagIngestService:
    def __init__(self, *, duplicate_of: int | None = None, error: Exception | None = None) -> None:
        self.duplicate_of = duplicate_of
        self.error = error
        self.calls: list[dict] = []

    async def ingest(
        self, *, source_path, document_type, budget, pseudonymize: bool = False
    ) -> IngestResponse:
        self.calls.append(
            {
                "source_path": source_path,
                "document_type": document_type,
                "budget": budget,
                "pseudonymize": pseudonymize,
            }
        )
        if self.duplicate_of is not None:
            raise DuplicateDocumentError(self.duplicate_of)
        if self.error is not None:
            raise self.error
        return IngestResponse(
            document_id=7,
            chunks_created=len(budget.components),
            embedding_dimension=1536,
            ingestion_time_ms=42,
        )


@pytest.fixture
def wired(client: TestClient):
    def _wire(service) -> TestClient:
        main.app.dependency_overrides[get_rag_ingest_service] = lambda: service
        return client

    yield _wire
    main.app.dependency_overrides.clear()


def test_ingest_persists_and_returns_contract(wired) -> None:
    fake = FakeRagIngestService()
    client = wired(fake)

    response = client.post(URL, json=ingest_payload())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "document_id": 7,
        "chunks_created": 1,
        "embedding_dimension": 1536,
        "ingestion_time_ms": 42,
        "entities_pseudonymized": 0,
    }
    # The router passes the validated Budget through, not the raw dict.
    assert fake.calls[0]["budget"].budget_id == "BUD-2024-001"
    assert fake.calls[0]["source_path"] == "data/budgets_sample.json::BUD-2024-001"
    assert fake.calls[0]["document_type"] == "historical_budget"


def test_ingest_accepts_a_budget_straight_from_the_sample_file(wired) -> None:
    fake = FakeRagIngestService()
    client = wired(fake)
    budget = first_sample_budget()

    response = client.post(
        URL,
        json={
            "source_path": f"data/budgets_sample.json::{budget['budget_id']}",
            "document_type": "historical_budget",
            "content": budget,
        },
    )

    assert response.status_code == 200, response.text
    assert fake.calls[0]["budget"].budget_id == budget["budget_id"]


def test_pseudonymize_defaults_to_false(wired) -> None:
    """The default must not change stored text — search distances depend on it."""
    fake = FakeRagIngestService()
    client = wired(fake)

    client.post(URL, json=ingest_payload())

    assert fake.calls[0]["pseudonymize"] is False


def test_pseudonymize_flag_reaches_the_service(wired) -> None:
    """If the router drops the flag, the client's PII request is silently ignored."""
    fake = FakeRagIngestService()
    client = wired(fake)

    client.post(URL, json={**ingest_payload(), "pseudonymize": True})

    assert fake.calls[0]["pseudonymize"] is True


def test_pseudonymize_must_be_boolean(wired) -> None:
    fake = FakeRagIngestService()
    client = wired(fake)

    response = client.post(URL, json={**ingest_payload(), "pseudonymize": "yes"})

    assert response.status_code == 422
    assert fake.calls == []


def test_pseudonymizer_unavailable_returns_503_not_a_plaintext_write(wired) -> None:
    """A deployment that cannot pseudonymize must refuse, never store plaintext."""
    from app.embedding_pipeline.ingest_service import PseudonymizationUnavailable

    client = wired(FakeRagIngestService(error=PseudonymizationUnavailable("not wired")))

    response = client.post(URL, json={**ingest_payload(), "pseudonymize": True})

    assert response.status_code == 503
    assert response.json() == {"detail": "Pseudonymization is not available."}


def test_entities_pseudonymized_is_reported(wired) -> None:
    """The response must say what happened, or the flag is unobservable."""
    class Reporting(FakeRagIngestService):
        async def ingest(self, **kwargs):
            real = await super().ingest(**kwargs)
            return real.model_copy(update={"entities_pseudonymized": 3})

    client = wired(Reporting())

    body = client.post(URL, json={**ingest_payload(), "pseudonymize": True}).json()

    assert body["entities_pseudonymized"] == 3


def test_ingest_duplicate_returns_409_with_literal_shape(wired) -> None:
    client = wired(FakeRagIngestService(duplicate_of=42))

    response = client.post(URL, json=ingest_payload())

    assert response.status_code == 409
    # Top-level shape from the exercise statement, NOT nested under "detail".
    assert response.json() == {"detail": "Document already ingested", "document_id": 42}


def test_ingest_service_unavailable_returns_500(wired) -> None:
    client = wired(None)

    response = client.post(URL, json=ingest_payload())

    assert response.status_code == 500
    assert response.json() == {"detail": "Embedding service is not available."}


def test_ingest_invalid_budget_returns_422_before_touching_the_service(wired) -> None:
    fake = FakeRagIngestService()
    client = wired(fake)
    payload = ingest_payload()
    payload["content"].pop("components")  # Budget requires >= 1 component

    response = client.post(URL, json=payload)

    assert response.status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        # The Session-7 batch body: gone, and it must fail loudly.
        {"budgets": [{"budget_id": "BUD-1"}]},
        {},
        {"source_path": "x", "document_type": "historical_budget"},
        {**ingest_payload(), "unknown": 1},
        {**ingest_payload(), "source_path": ""},
    ],
)
def test_invalid_payloads_return_422(wired, payload) -> None:
    fake = FakeRagIngestService()
    client = wired(fake)
    assert client.post(URL, json=payload).status_code == 422
    assert fake.calls == []


def test_unknown_sector_returns_422(wired) -> None:
    fake = FakeRagIngestService()
    client = wired(fake)
    payload = ingest_payload()
    payload["content"]["client_metadata"]["sector"] = "fintech"

    assert client.post(URL, json=payload).status_code == 422
    assert fake.calls == []


def test_embedding_failure_returns_generic_500_without_leaking_detail(wired) -> None:
    client = wired(
        FakeRagIngestService(error=RuntimeError("sk-secret-key exploded in provider"))
    )

    response = client.post(URL, json=ingest_payload())

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to generate embeddings."}
    assert "sk-secret" not in response.text
    assert "exploded" not in response.text


def test_dimension_mismatch_returns_generic_500_without_leaking_detail(wired) -> None:
    client = wired(
        FakeRagIngestService(
            error=EmbeddingDimensionMismatch("provider returned 768 dims, column is 1536")
        )
    )

    response = client.post(URL, json=ingest_payload())

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to generate embeddings."}
    assert "768" not in response.text


def test_route_is_documented_in_openapi(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert URL in spec["paths"]
    assert "post" in spec["paths"][URL]
    assert "409" in spec["paths"][URL]["post"]["responses"]
