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
    MAX_DESCRIPTION_CHARS,
    MAX_QUERY_CHARS,
    MAX_SEARCH_K,
    MAX_SOURCE_PATH_CHARS,
    Budget,
    BudgetComponent,
    Chunk,
    EmbeddedChunk,
    IngestRequest,
    SearchRequest,
)

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"
TRANSCRIPTS_DIR = Path(__file__).resolve().parent.parent / "examples" / "transcripts"

# The three probe transcripts shipped for the Session-9 trace. Two of them were
# rejected by the old 2_000-character cap, which is why it was raised.
TRANSCRIPT_FILES = ("01_clear.txt", "02_ambiguous.txt", "03_hard.txt")


def sample_budgets() -> list[dict]:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def sample_models() -> list[Budget]:
    """The sample corpus as validated models.

    The batch ``IngestRequest`` is gone (one document per request now), so the
    sample is validated per budget.
    """
    return [Budget.model_validate(budget) for budget in sample_budgets()]


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
    budgets = sample_models()
    assert len(budgets) == 17
    assert sum(len(b.components) for b in budgets) == 60


def test_sample_covers_every_declared_sector() -> None:
    sectors = {budget.client_metadata.sector for budget in sample_models()}
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


def test_duplicate_component_ids_are_rejected() -> None:
    payload = valid_budget_payload()
    payload["components"].append(dict(payload["components"][0]))
    with pytest.raises(ValidationError, match="component_id"):
        Budget.model_validate(payload)


def test_ingest_request_wraps_one_budget_and_rejects_unknown_fields() -> None:
    payload = {
        "source_path": "data/budgets_sample.json::BUD-2024-001",
        "document_type": "historical_budget",
        "content": valid_budget_payload(),
    }
    request = IngestRequest.model_validate(payload)
    assert request.content.budget_id == "BUD-2024-999"

    with pytest.raises(ValidationError):
        IngestRequest.model_validate({**payload, "budgets": [valid_budget_payload()]})


def test_the_session_7_batch_payload_is_rejected() -> None:
    """One document per request: the old batch body must fail loudly."""
    with pytest.raises(ValidationError):
        IngestRequest.model_validate({"budgets": [valid_budget_payload()]})


@pytest.mark.parametrize(
    "source_path",
    [
        "",
        "x" * (MAX_SOURCE_PATH_CHARS + 1),
        "has\ttab",
        "has\nnewline",
        "has\x00nul",
        " padded ",
    ],
)
def test_source_path_is_bounded_and_clean(source_path: str) -> None:
    with pytest.raises(ValidationError):
        IngestRequest.model_validate(
            {
                "source_path": source_path,
                "document_type": "historical_budget",
                "content": valid_budget_payload(),
            }
        )


def test_document_type_length_is_bounded() -> None:
    with pytest.raises(ValidationError):
        IngestRequest.model_validate(
            {
                "source_path": "x",
                "document_type": "d" * 51,
                "content": valid_budget_payload(),
            }
        )


def test_search_request_defaults_and_bounds() -> None:
    assert SearchRequest.model_validate({"query": "oauth"}).k == 5
    assert SearchRequest.model_validate({"query": "oauth", "k": MAX_SEARCH_K}).k == 50
    for bad_k in (0, -1, MAX_SEARCH_K + 1):
        with pytest.raises(ValidationError):
            SearchRequest.model_validate({"query": "oauth", "k": bad_k})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "x" * (MAX_QUERY_CHARS + 1)})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": ""})


def test_real_transcripts_fit_the_search_query_bound() -> None:
    """The Session-9 probe transcripts must be accepted as a ``/search`` query.

    The bound was raised from 2,000 to 20,000 characters for exactly this: the
    trace sends a whole meeting transcript, and two of the three files are
    longer than the old cap.
    """
    for name in TRANSCRIPT_FILES:
        text = (TRANSCRIPTS_DIR / name).read_text(encoding="utf-8")
        assert len(text) <= MAX_QUERY_CHARS, f"{name} is {len(text)} chars"
        request = SearchRequest.model_validate({"query": text, "k": 5})
        assert request.query == text
        assert request.k == 5


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


def test_search_request_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "oauth", "k": 3, "filter": "x"})


def test_component_tech_stack_cap_is_enforced() -> None:
    payload = valid_budget_payload()
    payload["components"][0]["tech_stack"] = ["x"] * 51
    with pytest.raises(ValidationError):
        BudgetComponent.model_validate(payload["components"][0])
