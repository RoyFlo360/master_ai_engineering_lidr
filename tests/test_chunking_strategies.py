"""Unit tests for the dependency-free chunking strategies (spec 013).

Hermetic: no provider, no NLTK download (the sentence tokenizer is stubbed), no
network. These pin the properties that make the comparison meaningful — the
structural output is unchanged, the window geometry is exact, and every strategy
emits the same metadata vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.embedding_pipeline.analysis.similarity import cosine_similarity, percentile
from app.embedding_pipeline.chunking.base import Chunker, count_tokens
from app.embedding_pipeline.chunking.structural import (
    JSONStructuralChunker,
    component_metadata,
    render_component_text,
    serialize_budget,
)
from app.embedding_pipeline.chunking.strategies.fixed_size import (
    CHUNK_SIZE_TOKENS,
    OVERLAP_TOKENS,
    FixedSizeChunker,
)
from app.embedding_pipeline.chunking.strategies.hierarchical import HierarchicalChunker
from app.embedding_pipeline.chunking.strategies.recursive import RecursiveChunker
from app.embedding_pipeline.schemas import Budget, BudgetComponent

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


def sample_budgets() -> list[Budget]:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    return [Budget.model_validate(budget) for budget in payload]


def one_budget() -> Budget:
    return sample_budgets()[0]


# --- the interface -----------------------------------------------------------


def test_every_strategy_implements_the_chunker_interface():
    for chunker in (JSONStructuralChunker(), FixedSizeChunker(), HierarchicalChunker(),
                    RecursiveChunker()):
        assert isinstance(chunker, Chunker)
        assert isinstance(chunker.strategy_name, str)
        assert chunker.strategy_name
        assert chunker.last_extra_api_calls == 0
        assert chunker.last_extra_cost_usd == 0.0


def test_the_module_level_import_still_works():
    """`app.embedding_pipeline.chunker` is the Session-7 import path."""
    from app.embedding_pipeline.chunker import JSONStructuralChunker as Legacy

    assert Legacy is JSONStructuralChunker


# --- structural: output is frozen -------------------------------------------


def test_structural_renders_the_documented_template():
    budget = one_budget()
    component = budget.components[0]
    text = render_component_text(budget, component)

    assert text.startswith(f"[Project: {budget.project_summary}]")
    assert "[Client sector: finance | Year: 2024 | Main tech:" in text
    assert f"Component: {component.name}" in text
    assert f"Description: {component.description}" in text
    assert "Estimated hours:" in text


def test_structural_metadata_keys_are_exact():
    budget = one_budget()
    chunk = JSONStructuralChunker().chunk([budget])[0]
    assert set(chunk.metadata) == {
        "budget_id",
        "component_id",
        "client_sector",
        "main_technology",
        "year",
        "complexity",
        "estimated_hours",
    }
    assert chunk.chunk_id == f"{budget.budget_id}::{budget.components[0].component_id}"


def test_structural_is_one_chunk_per_component_on_the_sample():
    chunks = JSONStructuralChunker().chunk(sample_budgets())
    assert len(chunks) == 60


def test_serialize_budget_contains_every_component():
    budget = one_budget()
    text = serialize_budget(budget)
    assert budget.project_summary in text
    for component in budget.components:
        assert component.name in text


def test_component_metadata_matches_the_chunk_metadata():
    budget = one_budget()
    assert component_metadata(budget, budget.components[0]) == (
        JSONStructuralChunker().chunk([budget])[0].metadata
    )


# --- fixed size: the geometry is exact --------------------------------------


def test_fixed_size_window_and_overlap():
    chunker = FixedSizeChunker()
    chunks = chunker.chunk(sample_budgets())

    assert chunks
    # No window exceeds the configured size ...
    assert max(chunk.token_count for chunk in chunks) <= CHUNK_SIZE_TOKENS
    # ... and the ids are stable and carry the part index.
    assert all("::p" in chunk.chunk_id for chunk in chunks)
    assert chunks[0].metadata["part"] == 0
    assert OVERLAP_TOKENS < CHUNK_SIZE_TOKENS


def test_fixed_size_splits_a_long_component_into_parts():
    long_description = " ".join(["authentication token rotation"] * 400)
    payload = one_budget().model_dump()
    payload["components"] = [
        {
            **payload["components"][0],
            "description": long_description,
        }
    ]
    budget = Budget.model_validate(payload)

    chunks = FixedSizeChunker().chunk([budget])

    assert len(chunks) > 1
    assert [chunk.metadata["part"] for chunk in chunks] == list(range(len(chunks)))


# --- recursive ---------------------------------------------------------------


def test_recursive_chunks_whole_budgets():
    chunks = RecursiveChunker().chunk(sample_budgets())
    assert chunks
    assert all("::r" in chunk.chunk_id for chunk in chunks)
    # Splitting a whole document yields fewer, larger pieces than per-component.
    assert len(chunks) < len(JSONStructuralChunker().chunk(sample_budgets()))


def test_recursive_respects_the_token_budget():
    chunks = RecursiveChunker().chunk(sample_budgets())
    # The splitter is token-sized; allow the documented overlap overshoot.
    assert max(chunk.token_count for chunk in chunks) < 600


# --- hierarchical ------------------------------------------------------------


def test_hierarchical_emits_a_parent_per_budget_and_a_child_per_component():
    budgets = sample_budgets()
    chunks = HierarchicalChunker().chunk(budgets)

    parents = [chunk for chunk in chunks if chunk.metadata["level"] == "parent"]
    children = [chunk for chunk in chunks if chunk.metadata["level"] == "child"]

    assert len(parents) == len(budgets)
    assert len(children) == sum(len(budget.components) for budget in budgets)
    assert all(child.metadata["parent_chunk_id"] == f"{child.metadata['budget_id']}::parent"
               for child in children)


def test_hierarchical_parent_text_is_the_whole_budget():
    budget = one_budget()
    chunks = HierarchicalChunker().chunk([budget])
    parent = next(chunk for chunk in chunks if chunk.metadata["level"] == "parent")
    assert parent.text == serialize_budget(budget)


# --- sentence window (stubbed tokenizer, no download) -----------------------


def test_sentence_window_indexes_sentences_and_carries_the_window(monkeypatch):
    import app.embedding_pipeline.chunking.strategies.sentence_window as module

    monkeypatch.setattr(module, "ensure_punkt", lambda: None)
    monkeypatch.setattr(
        module.nltk,
        "sent_tokenize",
        lambda text: ["First.", "Second.", "Third."],
    )

    budget = one_budget()
    chunks = module.SentenceWindowChunker().chunk([budget])

    assert len(chunks) == 3 * len(budget.components)
    first = chunks[0]
    # The embedded unit is the bare sentence; the window travels in metadata.
    assert first.text == "First."
    assert first.metadata["window_text"] == "First. Second. Third."
    assert first.metadata["sentence_index"] == 0


def test_sentence_window_raises_actionably_when_data_is_missing(monkeypatch):
    import app.embedding_pipeline.chunking.strategies.sentence_window as module

    def missing(_resource: str) -> None:
        raise LookupError("no data")

    def unavailable(*_args, **_kwargs) -> None:
        raise OSError("offline")

    monkeypatch.setattr(module.nltk.data, "find", missing)
    monkeypatch.setattr(module.nltk, "download", unavailable)

    with pytest.raises(RuntimeError, match="NLTK sentence data is missing"):
        module.ensure_punkt()


# --- shared helpers ----------------------------------------------------------


def test_count_tokens_is_positive_and_monotonic():
    assert count_tokens("hello world") > 0
    assert count_tokens("hello world " * 10) > count_tokens("hello world")


def test_cosine_similarity_edge_cases():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_percentile_interpolates_and_handles_empty():
    assert percentile([], 50) == 0.0
    assert percentile([5], 95) == 5.0
    assert percentile([1, 2, 3, 4, 5], 50) == pytest.approx(3.0)
    assert percentile([0, 10], 50) == pytest.approx(5.0)
    assert percentile([0, 10], 0) == 0.0
    assert percentile([0, 10], 100) == 10.0
