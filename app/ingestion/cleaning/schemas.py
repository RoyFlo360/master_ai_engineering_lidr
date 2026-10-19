"""Pandera v2 schema for the cleaned budget DataFrame.

Note the Pandera v2 import path: ``import pandera.pandas as pa``. In v1 it was
``import pandera as pa``. The migration broke many tutorials; the live session
calls this out explicitly.

``strict=False`` because the cleaned frame legitimately carries columns the
record contract does not own (``content_hash`` from the dedup step, ``phases``
from the source, the ``contact*`` pair). Those are preserved for downstream use
and simply are not part of the column contract.

``client_name`` is **not** nullable: a budget with no client is not usable as a
retrieval artefact, and the policy routes that failure to *quarantine* (owner
review) rather than *discard* — it is recoverable, the money is real.
"""
from __future__ import annotations

try:  # pandera >= 0.20 (v2 import path)
    import pandera.pandas as pa
    from pandera.pandas import Check, Column, DataFrameSchema
except ImportError:  # pragma: no cover - pandera < 0.20 compatibility
    import pandera as pa
    from pandera import Check, Column, DataFrameSchema

BUDGET_ID_PATTERN = r"^BUDGET-\d{4}-\d{4}$"
CLIENT_CODE_PATTERN = r"^CLI-\d{4}$"
ALLOWED_CURRENCIES = ("EUR", "USD", "GBP")
MAX_TOTAL_AMOUNT = 10_000_000.0

BudgetRecord: DataFrameSchema = DataFrameSchema(
    columns={
        "budget_id": Column(
            str,
            checks=Check.str_matches(BUDGET_ID_PATTERN),
            nullable=False,
            required=True,
        ),
        "client_name": Column(
            str,
            nullable=False,
            required=True,
        ),
        "client_code": Column(
            str,
            checks=Check.str_matches(CLIENT_CODE_PATTERN),
            nullable=False,
            required=True,
        ),
        "currency": Column(
            str,
            checks=Check.isin(list(ALLOWED_CURRENCIES)),
            nullable=False,
            required=True,
        ),
        "total_amount": Column(
            float,
            checks=[Check.ge(0), Check.le(MAX_TOTAL_AMOUNT)],
            nullable=False,
            required=True,
        ),
        "signed_at": Column(
            "datetime64[ns]",
            nullable=False,
            required=True,
        ),
    },
    strict=False,
    coerce=False,
)

__all__ = [
    "ALLOWED_CURRENCIES",
    "BUDGET_ID_PATTERN",
    "BudgetRecord",
    "CLIENT_CODE_PATTERN",
    "MAX_TOTAL_AMOUNT",
    "pa",
]
