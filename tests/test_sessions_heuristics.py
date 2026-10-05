"""Unit tests for the heuristic metadata extractor.

Pure string work: no LLM, no network, no session. The point is that the
heuristic is conservative — it fills a field only when a clear pattern appears,
and the estimation summary always backs ``agreed_scope``.
"""

import pytest

from app.schemas.new_estimation_schema import EstimationResult, Phase
from app.sessions.heuristics import extract_metadata_heuristic
from app.sessions.models import ProjectMetadata


def result(summary: str = "A marketplace app for second-hand furniture.") -> EstimationResult:
    return EstimationResult(
        summary=summary,
        confidence_pct=80,
        phases=[
            Phase(
                name="Discovery",
                duration_weeks=1,
                cost_eur=500,
                summary="Discovery workshops and scope definition.",
            )
        ],
        total_duration_weeks=1,
        total_cost_eur=500,
    )


def extract(transcript: str, summary: str | None = None) -> ProjectMetadata:
    return extract_metadata_heuristic(
        transcript=transcript,
        result=result() if summary is None else result(summary),
    )


# --- project name -----------------------------------------------------------


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("A marketplace app called Furnish, built for students.", "Furnish"),
        ("The project is named Acme Portal and launches in May.", "Acme Portal"),
        ("We want the Tennis Club platform to handle bookings.", "Tennis Club"),
        ("No name is given in this sentence.", None),
    ],
)
def test_project_name(transcript, expected):
    assert extract(transcript).project_name == expected


# --- team size --------------------------------------------------------------


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("We are a team of 4 working on this.", 4),
        ("There will be 3 developers and one designer.", 3),
        ("A 10-person team should be enough.", 10),
        ("Just me for now.", None),
        ("A team of 99 is unrealistic.", None),  # clamped out
    ],
)
def test_team_size(transcript, expected):
    assert extract(transcript).assumed_team_size == expected


# --- technologies -----------------------------------------------------------


def test_distinctive_technologies_match_case_insensitively():
    found = extract("we will use fastapi, postgresql and react").mentioned_technologies

    assert found == ["FastAPI", "React", "PostgreSQL"]


def test_ambiguous_terms_are_matched_case_sensitively():
    """'go' the verb is not the Go language; 'Go' is."""
    assert "Go" not in extract("we will go live next month").mentioned_technologies
    assert "Go" in extract("the backend is written in Go").mentioned_technologies


def test_technologies_are_deduplicated_and_ordered_by_vocabulary():
    found = extract("React, react and React Native").mentioned_technologies

    assert found == ["React", "React Native"]


def test_a_technology_only_in_the_estimation_text_is_still_found():
    found = extract_metadata_heuristic(
        transcript="just build the thing",
        result=result(summary="A Django service with a PostgreSQL database."),
    )

    assert "Django" in found.mentioned_technologies
    assert "PostgreSQL" in found.mentioned_technologies


# --- agreed scope -----------------------------------------------------------


def test_agreed_scope_is_the_latest_estimation_summary():
    scope = "A marketplace app for second-hand furniture."
    assert extract("anything", summary=scope).agreed_scope == scope


# --- conservatism and non-Latin input ---------------------------------------


def test_a_vague_turn_yields_only_the_scope():
    metadata = extract("It should be a nice thing.", summary="An estimation summary.")

    assert metadata.project_name is None
    assert metadata.assumed_team_size is None
    assert metadata.mentioned_technologies == []
    assert metadata.agreed_scope == "An estimation summary."


def test_non_latin_and_emoji_input_never_raises():
    metadata = extract("プロジェクト を 作る 🚀 — naïve café", summary="A project.")

    assert metadata.agreed_scope == "A project."
    assert metadata.mentioned_technologies == []
