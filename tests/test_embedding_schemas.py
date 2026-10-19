"""Schema tests for the embedding pipeline.

Everything here is hermetic: no provider, no network, no tokenizer download
(the schemas do not tokenize).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.embedding_pipeline.schemas import (
    MAX_CHUNKS_PER_REQUEST,
    MAX_DESCRIPTION_CHARS,
    Budget,
    BudgetComponent,
    Chunk,
    EmbeddedChunk,
    IngestRequest,
    IngestStats,
)

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


def sample_budgets() -> list[dict]:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def valid_budget_payload(**overrides) -> dict:
    payload = {
        "budget_id": "BUD-2024-999",
        "client_metadata": {"name": "Acme", "sector": "finance", "country": "ES"},
        "project_summary": "A small API.",
        "main_technology": "python_fastapi",
        "year": 2024,
        "total_estimated_hours": 10,
        "components": [
            {
                "component_id": "AUTH-001",
                "name": "Auth",
                "description": "Token-based authentication.",
                "tech_stack": ["python_fastapi"],
                "estimated_hours": 10,
                "complexity": "low",
                "dependencies": [],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_sample_file_parses_and_validates() -> None:
    request = IngestRequest.model_validate({"budgets": sample_budgets()})
    assert len(request.budgets) == 15
    assert sum(len(b.components) for b in request.budgets) == 52


def test_sample_covers_every_declared_sector() -> None:
    request = IngestRequest.model_validate({"budgets": sample_budgets()})
    sectors = {budget.client_metadata.sector for budget in request.budgets}
    assert sectors == {"finance", "ecommerce", "healthcare", "industrial"}


@pytest.mark.parametrize("sector", ["fintech", "", "FINANCE", "other"])
def test_unknown_sector_is_rejected(sector: str) -> None:
    payload = valid_budget_payload()
    payload["client_metadata"]["sector"] = sector
    with pytest.raises(ValidationError):
        Budget.model_validate(payload)


def test_unknown_complexity_is_rejected() -> None:
    payload = valid_budget_payload()
    payload["components"][0]["complexity"] = "extreme"
    with pytest.raises(ValidationError):
        Budget.model_validate(payload)


@pytest.mark.parametrize("year", [1999, 2101])
def test_year_out_of_range_is_rejected(year: int) -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate(valid_budget_payload(year=year))


def test_negative_hours_are_rejected() -> None:
    payload = valid_budget_payload()
    payload["components"][0]["estimated_hours"] = -1
    with pytest.raises(ValidationError):
        Budget.model_validate(payload)


def test_empty_components_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate(valid_budget_payload(components=[]))


def test_empty_budgets_are_rejected() -> None:
    with pytest.raises(ValidationError):
        IngestRequest.model_validate({"budgets": []})


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate(valid_budget_payload(secret="x"))


def test_unknown_request_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IngestRequest.model_validate(
            {"budgets": [valid_budget_payload()], "extra": True}
        )


def test_duplicate_budget_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="budget_id"):
        IngestRequest.model_validate(
            {"budgets": [valid_budget_payload(), valid_budget_payload()]}
        )


def test_duplicate_component_ids_are_rejected() -> None:
    payload = valid_budget_payload()
    payload["components"].append(dict(payload["components"][0]))
    with pytest.raises(ValidationError, match="component_id"):
        Budget.model_validate(payload)


def test_chunk_cap_is_enforced() -> None:
    budgets = []
    for index in range(6):
        payload = valid_budget_payload(budget_id=f"BUD-{index}")
        component = payload["components"][0]
        payload["components"] = [
            {**component, "component_id": f"C-{n}"} for n in range(100)
        ]
        budgets.append(payload)
    # 6 x 100 = 600 components > 500 chunk cap.
    assert MAX_CHUNKS_PER_REQUEST == 500
    with pytest.raises(ValidationError, match="cap"):
        IngestRequest.model_validate({"budgets": budgets})


def test_description_length_cap_is_enforced() -> None:
    payload = valid_budget_payload()
    payload["components"][0]["description"] = "x" * (MAX_DESCRIPTION_CHARS + 1)
    with pytest.raises(ValidationError):
        Budget.model_validate(payload)


def test_chunk_and_embedded_chunk_round_trip() -> None:
    chunk = Chunk(chunk_id="B::C", text="hello", metadata={"year": 2024}, token_count=1)
    embedded = EmbeddedChunk(**chunk.model_dump(), embedding=[0.1, 0.2])
    assert embedded.chunk_id == "B::C"
    assert embedded.metadata == {"year": 2024}
    assert embedded.embedding == [0.1, 0.2]


def test_ingest_stats_serializes_to_the_documented_keys() -> None:
    stats = IngestStats(
        total_budgets=15,
        total_chunks=52,
        total_tokens=4100,
        estimated_cost_usd=0.000082,
    )
    assert stats.model_dump() == {
        "total_budgets": 15,
        "total_chunks": 52,
        "total_tokens": 4100,
        "estimated_cost_usd": 0.000082,
    }


def test_component_tech_stack_cap_is_enforced() -> None:
    payload = valid_budget_payload()
    payload["components"][0]["tech_stack"] = ["x"] * 51
    with pytest.raises(ValidationError):
        BudgetComponent.model_validate(payload["components"][0])
