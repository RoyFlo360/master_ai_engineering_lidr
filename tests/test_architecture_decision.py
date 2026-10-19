"""The CAG-vs-RAG decision: AND semantics and the three recommendations."""
from __future__ import annotations

import pytest

from app.ingestion.architecture import (
    CURRENT_MODEL,
    PROYECTO_2,
    CAGViability,
    CorpusProfile,
    ModelProfile,
    _main,
    assess_cag_viability,
    recommend_architecture,
)

SMALL_MODEL = ModelProfile(
    name="gpt-4o-mini", context_window=128_000, cost_per_1k_input=0.00015, prefix_caching=True
)


def corpus(**overrides) -> CorpusProfile:
    base = {
        "name": "corpus",
        "estimated_tokens": 5_000,
        "refresh_frequency_days": 30,
        "traceability_required": False,
        "access_control_required": False,
    }
    base.update(overrides)
    return CorpusProfile(**base)


# --------------------------------------------------------------------------- #
# CAGViability is an AND of four constraints
# --------------------------------------------------------------------------- #
def test_all_four_green_is_viable():
    viability = CAGViability(True, True, True, True)

    assert viability.viable is True
    assert viability.failing_constraints() == []


@pytest.mark.parametrize(
    ("kwargs", "failing"),
    [
        ({"context_window_ok": False}, "context_window"),
        ({"cost_ok": False}, "cost"),
        ({"latency_ok": False}, "latency"),
        ({"lost_in_the_middle_ok": False}, "lost_in_the_middle"),
    ],
)
def test_one_red_constraint_kills_cag(kwargs, failing):
    flags = {
        "context_window_ok": True,
        "cost_ok": True,
        "latency_ok": True,
        "lost_in_the_middle_ok": True,
    }
    flags.update(kwargs)
    viability = CAGViability(**flags)

    assert viability.viable is False
    assert viability.failing_constraints() == [failing]


def test_assess_marks_a_large_corpus_non_viable():
    viability = assess_cag_viability(PROYECTO_2, CURRENT_MODEL)

    assert viability.viable is False
    assert "context_window" in viability.failing_constraints()


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #
def test_proyecto_2_recommends_rag():
    assert recommend_architecture(PROYECTO_2, CURRENT_MODEL) == "RAG"


def test_small_stable_corpus_recommends_cag():
    assert recommend_architecture(
        corpus(estimated_tokens=5_000, refresh_frequency_days=30), SMALL_MODEL
    ) == "CAG"


def test_traceability_requirement_alone_recommends_hybrid():
    assert recommend_architecture(
        corpus(estimated_tokens=5_000, refresh_frequency_days=30, traceability_required=True),
        SMALL_MODEL,
    ) == "Hybrid"


def test_access_control_requirement_alone_recommends_hybrid():
    assert recommend_architecture(
        corpus(estimated_tokens=5_000, refresh_frequency_days=30, access_control_required=True),
        SMALL_MODEL,
    ) == "Hybrid"


def test_weekly_refresh_breaks_the_cache_friendly_axis():
    assert recommend_architecture(
        corpus(estimated_tokens=5_000, refresh_frequency_days=1), SMALL_MODEL
    ) == "RAG"


def test_corpus_that_does_not_fit_the_window_recommends_rag():
    assert recommend_architecture(
        corpus(estimated_tokens=200_000, refresh_frequency_days=30), SMALL_MODEL
    ) == "RAG"


def test_recommendation_is_one_of_the_three_labels():
    assert recommend_architecture(corpus(), SMALL_MODEL) in {"CAG", "Hybrid", "RAG"}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_reports_rag_and_the_failing_constraint(capsys):
    assert _main(["python -m app.ingestion.architecture"]) == 0

    output = capsys.readouterr().out
    assert "Proyecto 2" in output
    assert "NO viable" in output
    assert "context_window" in output
    assert "Recomendación arquitectónica: RAG" in output
