"""Tests for the three LLM-backed chunking strategies (spec 013).

Fake clients only: no provider is called, no key is needed, nothing is downloaded.
The behaviours that matter are the cost accounting, the degradation when a call
fails, and that a failed call never silently drops content.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.embedding_pipeline.chunking.strategies.contextual_retrieval import (
    ContextualRetrievalChunker,
)
from app.embedding_pipeline.chunking.strategies.propositional import PropositionalChunker
from app.embedding_pipeline.chunking.strategies.semantic import SemanticChunker
from app.embedding_pipeline.schemas import Budget

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


def one_budget() -> Budget:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[0]
    return Budget.model_validate(payload)


def components(budget: Budget) -> int:
    return len(budget.components)


# --- propositional -----------------------------------------------------------


class FakeCompletions:
    def __init__(self, propositions: list[str], *, fail: bool = False) -> None:
        self.propositions = propositions
        self.fail = fail
        self.calls = 0

    def create_with_completion(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider exploded")
        result = SimpleNamespace(propositions=self.propositions)
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=500)
        return result, SimpleNamespace(usage=usage)


def make_propositional(completions: FakeCompletions) -> PropositionalChunker:
    chunker = PropositionalChunker.__new__(PropositionalChunker)
    chunker._model = "gpt-4o-mini"
    chunker._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return chunker


def test_propositional_makes_one_call_per_component():
    budget = one_budget()
    completions = FakeCompletions(["alpha", "beta"])
    chunker = make_propositional(completions)

    chunks = chunker.chunk([budget])

    assert completions.calls == components(budget)
    assert chunker.last_extra_api_calls == components(budget)
    assert len(chunks) == 2 * components(budget)
    assert chunks[0].chunk_id.endswith("::prop0")
    assert chunks[0].text == "alpha"
    assert chunks[0].metadata["proposition"] == 0


def test_propositional_reports_cost_from_usage():
    budget = one_budget()
    chunker = make_propositional(FakeCompletions(["alpha"]))

    chunker.chunk([budget])

    # 1000 input @ $0.15/1M + 500 output @ $0.60/1M, per call.
    per_call = 1000 / 1_000_000 * 0.15 + 500 / 1_000_000 * 0.60
    assert chunker.last_extra_cost_usd == pytest.approx(per_call * components(budget))


def test_propositional_degrades_to_the_component_when_the_call_fails(monkeypatch):
    budget = one_budget()
    chunker = make_propositional(FakeCompletions([], fail=True))

    chunks = chunker.chunk([budget])

    # One chunk per component, carrying the full rendered text: nothing dropped.
    assert len(chunks) == components(budget)
    assert budget.components[0].name in chunks[0].text
    assert chunker.last_extra_cost_usd == 0.0


# --- contextual retrieval ----------------------------------------------------


class FakeMessages:
    def __init__(self, context: str, *, fail: bool = False, usage: dict | None = None) -> None:
        self.context = context
        self.fail = fail
        self.calls = 0
        self.usage = usage or {
            "input_tokens": 2000,
            "output_tokens": 50,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 0,
        }

    def create(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("anthropic exploded")
        return SimpleNamespace(
            content=[SimpleNamespace(text=self.context)],
            usage=SimpleNamespace(**self.usage),
        )


def make_contextual(messages: FakeMessages) -> ContextualRetrievalChunker:
    chunker = ContextualRetrievalChunker.__new__(ContextualRetrievalChunker)
    chunker._model = "claude-sonnet-4-5"
    chunker._client = SimpleNamespace(messages=messages)
    return chunker


def test_contextual_prepends_the_generated_context():
    budget = one_budget()
    messages = FakeMessages("This chunk belongs to a banking project.")
    chunker = make_contextual(messages)

    chunks = chunker.chunk([budget])

    assert messages.calls == components(budget)
    assert chunks[0].text.startswith("This chunk belongs to a banking project.")
    assert chunks[0].metadata["generated_context"] == "This chunk belongs to a banking project."
    # The id stays structural: one enriched chunk per component.
    assert chunks[0].chunk_id == f"{budget.budget_id}::{budget.components[0].component_id}"


def test_contextual_reports_cost_including_cache_tokens():
    budget = one_budget()
    chunker = make_contextual(FakeMessages("ctx"))

    chunker.chunk([budget])

    per_call = (
        2000 / 1_000_000 * 3.00
        + 50 / 1_000_000 * 15.00
        + 1000 / 1_000_000 * 3.75
        + 0 / 1_000_000 * 0.30
    )
    assert chunker.last_extra_cost_usd == pytest.approx(per_call * components(budget))


def test_contextual_degrades_to_the_plain_component_when_the_call_fails():
    budget = one_budget()
    chunker = make_contextual(FakeMessages("", fail=True))

    chunks = chunker.chunk([budget])

    assert len(chunks) == components(budget)
    assert chunks[0].metadata["generated_context"] == ""
    assert budget.components[0].name in chunks[0].text
    assert chunker.last_extra_cost_usd == 0.0


# --- semantic ----------------------------------------------------------------


class FakeSplitter:
    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.texts: list[str] = []

    def split_text(self, text: str) -> list[str]:
        self.texts.append(text)
        return self.pieces


def make_semantic(pieces: list[str]) -> SemanticChunker:
    chunker = SemanticChunker.__new__(SemanticChunker)
    chunker._embeddings = None
    chunker._splitter = FakeSplitter(pieces)
    return chunker


def test_semantic_requires_a_key():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        SemanticChunker(api_key=None)


def test_semantic_emits_one_chunk_per_piece_and_prices_the_ingestion_pass():
    budget = one_budget()
    chunker = make_semantic(["first piece", "second piece"])

    chunks = chunker.chunk([budget])

    assert len(chunks) == 2
    assert chunks[0].chunk_id == f"{budget.budget_id}::sem0"
    # The extra cost is one embedding pass over the serialized document.
    assert chunker.last_extra_api_calls == 1
    assert chunker.last_extra_cost_usd > 0.0
