"""Smoke test of the stress runner against an in-process scripted LLM.

The full runner orchestration (scenario walk + CSV writer + metric calls) is
exercised end-to-end with a scripted wrapper, so wiring bugs in the
``observation`` plumbing, the CSV schema, or the metric calls surface in
seconds without burning real LLM credit.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_estimation_service, get_session_store
from app.main import app
from app.schemas.new_estimation_schema import (
    DetailLevel,
    EstimationResult,
    OutputFormat,
    Phase,
    ProjectType,
)
from app.services.estimation import EstimationService
from app.sessions.models import ProjectMetadata
from app.sessions.store import SessionStore
from evals.stress.metrics import CostBudgetMetric, LatencyBudgetMetric
from evals.stress.run import _CSV_COLUMNS, _run_one_session
from evals.stress.scenarios import (
    DEFAULT_TURN_COUNTS,
    Scenario,
    ScenarioTurn,
    all_scenarios,
)


def _meta() -> dict:
    return {"model": "deepseek-flash", "provider": "deepseek", "latency_ms": 1}


class ScriptedWrapper:
    def __init__(self) -> None:
        self.primary_model = "deepseek/deepseek-flash"

    def complete_structured_chat(self, *, messages, response_model, **kwargs):
        if response_model is EstimationResult:
            return (
                EstimationResult(
                    summary="Nimbus procurement SaaS with React and Postgres.",
                    confidence_pct=80,
                    phases=[Phase(name="Build", duration_weeks=2, cost_eur=500, summary="Build the thing.")],
                    total_duration_weeks=2,
                    total_cost_eur=500,
                ),
                _meta(),
            )
        if response_model is ProjectMetadata:
            return ProjectMetadata(), _meta()
        # The compression summarizer's private ``_SummaryEnvelope``.
        return SimpleNamespace(summary="Earlier turns summarised."), _meta()


@pytest.fixture
def conversational_client():
    wrapper = ScriptedWrapper()
    store = SessionStore(max_turns=6)
    service = EstimationService(
        llm_wrapper=wrapper,
        exact_cache=None,
        semantic_cache=None,
        openai_client=None,
        metadata_extractor_model="gpt-4o-mini",
    )
    app.dependency_overrides[get_estimation_service] = lambda: service
    app.dependency_overrides[get_session_store] = lambda: store
    with TestClient(app) as client:
        yield client, store
    app.dependency_overrides.clear()


def _tiny_scenario() -> Scenario:
    """A scenario short enough to keep tests fast but long enough to exercise
    the sliding window + at least one drift evaluation post-turn-1."""

    # Build 20 minimally valid turns (the scenario dataclass enforces >=20).
    turns = [
        ScenarioTurn(
            transcript=(
                "We want a B2B SaaS called Nimbus for procurement teams. "
                "Stack: React + Postgres. Team of 3."
            ),
            fact_introduced="Nimbus",
            fact_field="project_name",
        )
    ]
    for i in range(2, 21):
        turns.append(
            ScenarioTurn(
                transcript=(
                    f"Turn {i}: add another feature, expand the scope a little. "
                    "Keep the same stack and team for now."
                ),
                fact_introduced=None,
            )
        )
    return Scenario(
        name="growing",
        project_type=ProjectType.WEB_SAAS,
        detail_level=DetailLevel.MEDIUM,
        output_format=OutputFormat.PHASES_TABLE,
        turns=turns,
    )


def test_runner_writes_csv_with_expected_columns_and_rows(
    conversational_client, tmp_path: Path
):
    """Walk 3 turns of a scripted session, validate CSV shape."""
    client, _store = conversational_client
    scenario = _tiny_scenario()

    csv_path = tmp_path / "smoke.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        rows_written = _run_one_session(
            client=client,
            scenario=scenario,
            size_kb=0,
            repeat=0,
            turns_to_walk=3,
            turn_set={1, 3},
            latency_metric=LatencyBudgetMetric(budget_ms=60_000),
            cost_metric=CostBudgetMetric(budget_usd=10.0),
            writer=writer,
        )

    assert rows_written == 3
    with csv_path.open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3
    for row in rows:
        assert row["scenario"] == "growing"
        assert row["turns_planned"] == "3"
        assert row["attachment_size_kb"] == "0"
        assert row["turn1_fact"] == "Nimbus"
        assert row["error"] == ""
        assert int(row["turn_index"]) >= 1
        assert int(row["messages_in_window"]) > 0
        assert row["latency_budget_passed"] == "True"
        assert row["cost_budget_passed"] == "True"
        assert row["n_in_set"] in {"True", "False"}

    # Turn 1 → drift blank (trivial); turns 2 & 3 → must be filled.
    assert rows[0]["memory_drift_passed"] == ""
    assert rows[0]["fact_recall_den"] == "0"
    assert rows[1]["memory_drift_passed"] in {"True", "False"}
    assert rows[2]["memory_drift_passed"] in {"True", "False"}
    # The turn-1 "Nimbus" fact is tracked from turn 2 onward.
    assert rows[1]["fact_recall_den"] == "1"


def test_runner_records_error_row_on_failure(conversational_client, tmp_path: Path):
    """If a turn raises, the runner records an error row and stops the session."""
    client, _store = conversational_client
    scenario = _tiny_scenario()
    # Replace turn 2's transcript with one short enough to trigger the
    # ``min_length=20`` validation on the form — that's a 422 from the API.
    bad_turn = ScenarioTurn(transcript="too short", fact_introduced=None)
    object.__setattr__(scenario, "turns", [scenario.turns[0], bad_turn, scenario.turns[2]])

    csv_path = tmp_path / "smoke.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        rows_written = _run_one_session(
            client=client,
            scenario=scenario,
            size_kb=0,
            repeat=0,
            turns_to_walk=3,
            turn_set={1, 3},
            latency_metric=LatencyBudgetMetric(budget_ms=60_000),
            cost_metric=CostBudgetMetric(budget_usd=10.0),
            writer=writer,
        )

    assert rows_written == 2  # turn 1 ok + turn 2 errored, then aborted
    with csv_path.open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["error"] == ""
    assert rows[1]["error"] != ""
    assert "HTTPStatusError" in rows[1]["error"] or "422" in rows[1]["error"]


# --- Block 2: N-turn truncation --------------------------------------------


def test_scenarios_support_the_brief_turn_counts():
    assert DEFAULT_TURN_COUNTS == (1, 3, 6, 10, 20)
    for scenario in all_scenarios():
        assert len(scenario.turns) >= 20
        for n in DEFAULT_TURN_COUNTS:
            view = scenario.truncated(n)
            assert len(view.turns) == n
            # The truncated view keeps the scenario's identity.
            assert view.name == scenario.name
            assert view.project_type == scenario.project_type


def test_truncated_beyond_length_returns_self():
    scenario = all_scenarios()[0]
    assert scenario.truncated(999) is scenario
    with pytest.raises(ValueError):
        scenario.truncated(0)
