"""Chunker tests: structure preservation, header text, metadata, token counts."""

from __future__ import annotations

import json
from pathlib import Path

import tiktoken

from app.embedding_pipeline.chunker import JSONStructuralChunker
from app.embedding_pipeline.schemas import Budget

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


def sample_budgets() -> list[Budget]:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    return [Budget.model_validate(budget) for budget in payload]


def one_budget() -> Budget:
    return Budget.model_validate(
        {
            "budget_id": "BUD-2024-001",
            "client_metadata": {
                "name": "FintechCorp",
                "sector": "finance",
                "country": "ES",
            },
            "project_summary": "Mobile banking API with OAuth 2.0.",
            "main_technology": "ruby_on_rails",
            "year": 2024,
            "total_estimated_hours": 120,
            "components": [
                {
                    "component_id": "AUTH-001",
                    "name": "OAuth 2.0 authentication backend",
                    "description": "Authorization code and refresh token flows.",
                    "tech_stack": ["ruby_on_rails", "redis"],
                    "estimated_hours": 120,
                    "complexity": "high",
                    "dependencies": [],
                }
            ],
        }
    )


def test_one_component_becomes_one_chunk() -> None:
    chunker = JSONStructuralChunker()
    budgets = sample_budgets()
    chunks = chunker.chunk(budgets)
    assert len(chunks) == 60
    assert len(chunks) == sum(len(b.components) for b in budgets)


def test_chunk_order_matches_input_order() -> None:
    budgets = sample_budgets()
    chunks = JSONStructuralChunker().chunk(budgets)
    expected = [
        f"{budget.budget_id}::{component.component_id}"
        for budget in budgets
        for component in budget.components
    ]
    assert [chunk.chunk_id for chunk in chunks] == expected


def test_text_has_the_contextual_header_and_component_detail() -> None:
    chunk = JSONStructuralChunker().chunk([one_budget()])[0]
    assert chunk.text.startswith("[Project: Mobile banking API with OAuth 2.0.]")
    assert (
        "[Client sector: finance | Year: 2024 | Main tech: ruby_on_rails]"
        in chunk.text
    )
    assert "Component: OAuth 2.0 authentication backend" in chunk.text
    assert "Description: Authorization code and refresh token flows." in chunk.text
    assert "Tech stack: ruby_on_rails, redis" in chunk.text
    assert "Complexity: high" in chunk.text
    assert "Estimated hours: 120" in chunk.text


def test_metadata_carries_the_seven_filterable_fields() -> None:
    chunk = JSONStructuralChunker().chunk([one_budget()])[0]
    assert chunk.metadata == {
        "budget_id": "BUD-2024-001",
        "component_id": "AUTH-001",
        "client_sector": "finance",
        "main_technology": "ruby_on_rails",
        "year": 2024,
        "complexity": "high",
        "estimated_hours": 120,
    }


def test_metadata_is_not_embedded_in_the_text() -> None:
    """The description is embedded; the filterable metadata keys are not."""
    chunk = JSONStructuralChunker().chunk([one_budget()])[0]
    assert "client_sector" not in chunk.text
    assert "component_id" not in chunk.text


def test_chunk_id_format() -> None:
    chunk = JSONStructuralChunker().chunk([one_budget()])[0]
    assert chunk.chunk_id == "BUD-2024-001::AUTH-001"


def test_token_count_matches_a_fresh_encoding() -> None:
    chunk = JSONStructuralChunker().chunk([one_budget()])[0]
    encoding = tiktoken.encoding_for_model("text-embedding-3-small")
    assert chunk.token_count == len(encoding.encode(chunk.text))
    assert chunk.token_count > 0


def test_chunking_is_deterministic() -> None:
    first = JSONStructuralChunker().chunk([one_budget()])
    second = JSONStructuralChunker().chunk([one_budget()])
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]


def test_a_long_description_is_not_split() -> None:
    budget = one_budget()
    long_description = "detail " * 5_000
    component = budget.components[0].model_copy(
        update={"description": long_description.strip()}
    )
    budget = budget.model_copy(update={"components": [component]})

    chunks = JSONStructuralChunker().chunk([budget])
    assert len(chunks) == 1
    assert long_description.strip() in chunks[0].text


def test_empty_input_yields_no_chunks() -> None:
    assert JSONStructuralChunker().chunk([]) == []


def test_free_text_cannot_alter_the_metadata() -> None:
    """A description that looks like JSON stays text, not structured input."""
    budget = one_budget()
    hostile = '{"budget_id": "HACKED", "component_id": "HACKED-001"}'
    component = budget.components[0].model_copy(update={"description": hostile})
    budget = budget.model_copy(update={"components": [component]})

    chunk = JSONStructuralChunker().chunk([budget])[0]
    assert chunk.metadata["budget_id"] == "BUD-2024-001"
    assert chunk.metadata["component_id"] == "AUTH-001"
    assert chunk.chunk_id == "BUD-2024-001::AUTH-001"
    assert hostile in chunk.text
