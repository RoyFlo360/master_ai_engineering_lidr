"""Evaluation harness for the conversational estimator.

Three pieces:

- ``dataset.py``: typed ``GoldenCase`` records and a JSON loader.
- ``metrics.py``: deterministic metrics (schema adherence, cost bounds,
  content recall). DeepEval's LLM-based ``GEval`` is opt-in.
- ``run.py``: CLI that fires each case at one of the two modes (actor / acb)
  and prints a comparative table.

The pre-shipped ``golden_dataset.json`` carries 16 cases mixing project
types, scope levels, NDA/regulatory flavours, tier expectations, and a couple
of out-of-scope adversarials.
"""

from evals.dataset import GoldenCase, load_dataset
from evals.metrics import (
    ContentRecallMetric,
    CostBoundsMetric,
    MetricResult,
    SchemaAdherenceMetric,
    run_all_metrics,
)

__all__ = [
    "GoldenCase",
    "load_dataset",
    "MetricResult",
    "SchemaAdherenceMetric",
    "CostBoundsMetric",
    "ContentRecallMetric",
    "run_all_metrics",
]
