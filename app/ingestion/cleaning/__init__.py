"""Cleaning and validation layer.

Two passes:

* ``budget_records.clean_budget_records`` — pandas-level fixes (null
  placeholders, currency casing, permissive coercion, hash-based dedup).
* ``policy.validate_with_policy`` — the Pandera ``BudgetRecord`` contract plus
  the routing policy that partitions rows into valid / quarantined / discarded.
  ``policy.clean_and_validate`` composes the two; that is what the orchestrator
  calls (lazily, so importing the app does not import pandas).

The split is deliberate: cleaning shapes the data, validation gatekeeps it.
"""
from app.ingestion.cleaning.budget_records import clean_budget_records
from app.ingestion.cleaning.policy import (
    ValidationResult,
    clean_and_validate,
    validate_with_policy,
)
from app.ingestion.cleaning.schemas import BudgetRecord

__all__ = [
    "BudgetRecord",
    "ValidationResult",
    "clean_and_validate",
    "clean_budget_records",
    "validate_with_policy",
]
