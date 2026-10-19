"""Validation policy router: reparar / cuarentena / descartar.

The Pandera schema can raise on the first failing check, but in production we
want the *full* list of failures — that is what tells the operator which dirt
family hit the run. We run it with ``lazy=True`` so Pandera collects all
failures, and its ``failure_cases`` are summarised in the report under
``failures_by_check`` / ``failures_by_column``.

Routing each failing row into one of three buckets is a *business* decision, not
a schema decision, so it lives here:

* ``valid``       — passes every check.
* ``quarantined`` — recoverable failure: the row is real data with a hole a
  human can fill (today: a missing ``client_name``, e.g. the seed record whose
  name is ``"TBD"``). Kept for owner review, kept **out** of the document
  stream.
* ``discarded``   — fatal failure: the row is structurally wrong or its numbers
  cannot be trusted (negative or absurd ``total_amount``, malformed
  ``budget_id``/``client_code``, unknown currency, unparseable ``signed_at``).
  Logged but not retried.

**Session 7 deviation from the reference.** The reference routes by substring
matching Pandera's *check names* (``"not_nullable"``, ``"in_range"``, …). Those
names are engine-version specific — they moved between pandera 0.19 and 0.3x —
so routing on them is a silent-breakage trap. This module routes on the
*column* that failed instead, which is stable across versions, and keeps
Pandera's per-check counts in the report for observability. The declared
contract (``schemas.BudgetRecord``) is still what decides whether a row is
valid; the classifier below encodes the same constraints explicitly so the
partition cannot drift with a library upgrade.

The ``report`` dict is JSON-serializable so it can sit next to the
``ingestion_jobs`` row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

from app.ingestion.cleaning.budget_records import clean_budget_records
from app.ingestion.cleaning.schemas import (
    ALLOWED_CURRENCIES,
    BUDGET_ID_PATTERN,
    CLIENT_CODE_PATTERN,
    MAX_TOTAL_AMOUNT,
    BudgetRecord,
    pa,
)

#: A failure in these columns is recoverable by a human → quarantine.
QUARANTINE_COLUMNS = {"client_name"}
#: A failure in these columns makes the row unusable → discard.
DISCARD_COLUMNS = {"budget_id", "client_code", "currency", "total_amount", "signed_at"}

Route = Literal["valid", "quarantined", "discarded"]

_BUDGET_ID_RE = re.compile(BUDGET_ID_PATTERN)
_CLIENT_CODE_RE = re.compile(CLIENT_CODE_PATTERN)


@dataclass
class ValidationResult:
    valid: pd.DataFrame
    quarantined: pd.DataFrame
    discarded: pd.DataFrame
    report: dict[str, Any] = field(default_factory=dict)


def clean_and_validate(records) -> ValidationResult:
    """Compose cleaning + validation — the entry point the orchestrator uses."""
    return validate_with_policy(clean_budget_records(records))


def validate_with_policy(
    df: pd.DataFrame, schema: Any = BudgetRecord
) -> ValidationResult:
    """Run :data:`BudgetRecord` against ``df`` and partition the rows."""
    if df is None or df.empty:
        empty = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        return ValidationResult(
            valid=empty,
            quarantined=empty.iloc[0:0].copy(),
            discarded=empty.iloc[0:0].copy(),
            report={
                "input_rows": 0,
                "valid_rows": 0,
                "quarantined_rows": 0,
                "discarded_rows": 0,
                "failures_by_check": {},
                "failures_by_column": {},
                "routed": [],
            },
        )

    failures_by_check, failures_by_column = _pandera_failures(df, schema)

    verdicts: list[Route] = []
    routed: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        route, reasons = _classify(row)
        verdicts.append(route)
        if route != "valid":
            routed.append(
                {
                    "budget_id": _as_text(row.get("budget_id")),
                    "route": route,
                    "reasons": reasons,
                }
            )

    route_series = pd.Series(verdicts, index=df.index, dtype="object")
    valid_mask = route_series == "valid"
    quarantined_mask = route_series == "quarantined"
    discarded_mask = route_series == "discarded"

    return ValidationResult(
        valid=df.loc[valid_mask].copy(),
        quarantined=df.loc[quarantined_mask].copy(),
        discarded=df.loc[discarded_mask].copy(),
        report={
            "input_rows": int(len(df)),
            "valid_rows": int(valid_mask.sum()),
            "quarantined_rows": int(quarantined_mask.sum()),
            "discarded_rows": int(discarded_mask.sum()),
            "failures_by_check": failures_by_check,
            "failures_by_column": failures_by_column,
            "routed": routed,
        },
    )


def _classify(row: pd.Series) -> tuple[Route, list[str]]:
    """Classify one cleaned row. Discard wins over quarantine."""
    discard: list[str] = []
    quarantine: list[str] = []

    budget_id = _as_text(row.get("budget_id"))
    if budget_id is None or not _BUDGET_ID_RE.match(budget_id):
        discard.append("budget_id")

    client_code = _as_text(row.get("client_code"))
    if client_code is None or not _CLIENT_CODE_RE.match(client_code):
        discard.append("client_code")

    currency = _as_text(row.get("currency"))
    if currency is None or currency.upper() not in ALLOWED_CURRENCIES:
        discard.append("currency")

    amount = _as_float(row.get("total_amount"))
    if amount is None or amount < 0 or amount > MAX_TOTAL_AMOUNT:
        discard.append("total_amount")

    if _is_missing(row.get("signed_at")):
        discard.append("signed_at")

    if _is_missing(row.get("client_name")):
        quarantine.append("client_name")

    if discard:
        return "discarded", discard + quarantine
    if quarantine:
        return "quarantined", quarantine
    return "valid", []


def _pandera_failures(
    df: pd.DataFrame, schema: Any
) -> tuple[dict[str, int], dict[str, int]]:
    """Summarise Pandera's lazy failures; never let a schema raise here.

    The partition is decided by :func:`_classify`; this is observability. If
    Pandera cannot even evaluate the frame (a dtype it does not know in this
    pandas version, say), the run still routes every row deterministically.
    """
    try:
        schema.validate(df, lazy=True)
        return {}, {}
    except pa.errors.SchemaErrors as err:
        cases = err.failure_cases
    except pa.errors.SchemaError as err:  # non-lazy fallback
        return {str(err.check): 1}, {str(getattr(err, "column", "unknown")): 1}
    except Exception:  # noqa: BLE001 - observability must never break the run
        return {"schema_unavailable": 1}, {}

    if cases is None:
        return {}, {}

    by_check: dict[str, int] = {}
    by_column: dict[str, int] = {}
    for _, case in cases.iterrows():
        check = str(case.get("check", "unknown"))
        column = str(case.get("column", "unknown"))
        by_check[check] = by_check.get(check, 0) + 1
        by_column[column] = by_column.get(column, 0) + 1
    return by_check, by_column


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):  # array-like
        return False


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        return None
    return str(value).strip()


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
