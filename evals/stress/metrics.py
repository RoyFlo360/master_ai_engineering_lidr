"""Three new metrics for the Session 6 stress exercise.

These live apart from ``evals.metrics`` because they operate over a
``TurnObservation`` (telemetry attached to the conversational response) and
a session snapshot dict (the JSON shape of ``GET /sessions/{id}``), not
over ``(GoldenCase, EstimationResult)``. The signature mismatch is the only
reason for the separate module — the ``MetricResult`` dataclass is reused
verbatim from ``evals.metrics`` to keep the report shape uniform.

Design choice: deterministic checks only. No embeddings, no LLM-as-judge.
``MemoryDriftMetric`` does a case-insensitive substring match against the
text of the session snapshot. That is enough to answer the question the
exercise actually asks ("at turn N, does the project_name from turn 1 still
survive somewhere?") and it has no failure modes of its own — when it says
the fact is gone, the fact is gone.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from app.schemas.new_estimation_schema import TurnObservation
from evals.metrics import MetricResult


FactField = Literal["project_name", "technologies", "scope", "summary", "any"]

# Where ``MemoryDriftMetric`` looks. The exercise's default set.
MemoryLocation = Literal["summary", "anchors", "metadata", "any"]
DEFAULT_MEMORY_LOCATIONS: tuple[str, ...] = ("summary", "anchors", "metadata")


class LatencyBudgetMetric:
    """Per-turn latency under a wall-clock budget.

    The runner instantiates this once with a budget (e.g. 8000 ms reflecting
    a customer-facing SLA) and calls ``evaluate`` for every turn.
    """

    name = "latency_budget"

    def __init__(self, budget_ms: int) -> None:
        if budget_ms <= 0:
            raise ValueError("budget_ms must be positive")
        self.budget_ms = budget_ms

    def evaluate(self, observation: TurnObservation) -> MetricResult:
        passed = observation.latency_ms <= self.budget_ms
        return MetricResult(
            name=self.name,
            score=1.0 if passed else 0.0,
            passed=passed,
            details=f"{observation.latency_ms} ms vs budget {self.budget_ms} ms",
        )


class CostBudgetMetric:
    """Per-turn cost under a USD budget.

    Reads ``cost_usd`` from the observation (already computed by the LLM
    wrapper from ``MODEL_COSTS`` and the actual token counts of this turn).
    The budget tests the cost of a *single* turn, not the cumulative spend
    across the session — that aggregate is computed by the runner from the
    per-turn observations.
    """

    name = "cost_budget"

    def __init__(self, budget_usd: float) -> None:
        if budget_usd <= 0:
            raise ValueError("budget_usd must be positive")
        self.budget_usd = budget_usd

    def evaluate(self, observation: TurnObservation) -> MetricResult:
        passed = observation.cost_usd <= self.budget_usd
        return MetricResult(
            name=self.name,
            score=1.0 if passed else 0.0,
            passed=passed,
            details=f"${observation.cost_usd:.6f} vs budget ${self.budget_usd:.6f}",
        )


class MemoryDriftMetric:
    """Does a fact introduced in an earlier turn still appear in the session?

    Signature follows the brief::

        MemoryDriftMetric(fact, where=["summary", "anchors", "metadata"])

    ``where`` names the slots of the ``GET /sessions/{id}`` snapshot to search:

    - ``"summary"``   — the rolling conversation summary text.
    - ``"anchors"``   — the verbatim anchor turns (``anchor_texts``).
    - ``"metadata"``  — ``project_name`` + ``mentioned_technologies`` + ``agreed_scope``.
    - ``"any"``       — the whole snapshot serialised (narrow selectors below too).

    Legacy single-field selectors are still accepted for convenience:
    ``project_name`` / ``technologies`` / ``scope`` / ``summary`` / ``any``
    (via ``where=[...]`` or the ``fact_field`` keyword).

    Matching is case-insensitive substring, deterministic — no embeddings,
    no LLM-as-judge.
    """

    name = "memory_drift"

    def __init__(
        self,
        fact: str,
        where: list[str] | None = None,
        *,
        fact_field: str | None = None,
    ) -> None:
        if not fact:
            raise ValueError("fact must be a non-empty string")
        if where is None and fact_field:
            where = [fact_field]
        self.fact = fact
        self.needle = fact.lower()
        self.where = list(where) if where else list(DEFAULT_MEMORY_LOCATIONS)

    def evaluate(self, snapshot: dict[str, Any]) -> MetricResult:
        haystack = self._haystack(snapshot)
        found = self.needle in haystack.lower()
        return MetricResult(
            name=self.name,
            score=1.0 if found else 0.0,
            passed=found,
            details=(
                f"fact={self.fact!r} where={self.where} "
                f"{'present' if found else 'missing'}"
            ),
        )

    def _haystack(self, snapshot: dict[str, Any]) -> str:
        """Join the text of every selected location into one search string."""
        metadata = snapshot.get("metadata") or {}
        parts: list[str] = []
        for loc in self.where:
            if loc == "summary":
                parts.append(str(snapshot.get("summary") or ""))
            elif loc == "anchors":
                anchors = snapshot.get("anchor_texts") or snapshot.get("anchors") or []
                if isinstance(anchors, list):
                    parts.append(
                        " ".join(
                            str(a.get("content", "")) if isinstance(a, dict) else str(a)
                            for a in anchors
                        )
                    )
                else:
                    parts.append(str(anchors))
            elif loc == "metadata":
                parts.append(str(metadata.get("project_name") or ""))
                parts.append(" ".join(metadata.get("mentioned_technologies") or []))
                parts.append(str(metadata.get("agreed_scope") or ""))
            elif loc == "project_name":
                parts.append(str(metadata.get("project_name") or ""))
            elif loc == "technologies":
                parts.append(" ".join(metadata.get("mentioned_technologies") or []))
            elif loc == "scope":
                parts.append(str(metadata.get("agreed_scope") or ""))
            else:  # "any" or anything unknown — fall back to the whole snapshot.
                parts.append(json.dumps(snapshot, default=str))
        return " ".join(parts)
