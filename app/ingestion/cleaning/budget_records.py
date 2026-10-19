"""Reparable cleaning for the budget tabular view.

Five steps, applied to a DataFrame whose schema we treat as **raw**:

1. **Null placeholders** — ``"TBD"``, ``"N/A"``, empty strings → ``pd.NA``.
   Disguised nulls are the easiest dirt to miss and the hardest to debug
   downstream; we normalize at the boundary.

2. **Currency casing** — ``EUR`` vs ``eur`` collapses to upper. Any other
   currency is preserved (validation will catch it).

3. **Date coercion** — ``signed_at`` can come as ISO ``2024-05-02`` or as the
   Spanish ``12/03/2024``. ``pd.to_datetime(dayfirst=True, errors="coerce")``
   returns ``NaT`` for the unparseable rows; validation routes them.

4. **Numeric coercion** — ``total_amount`` coerced to numeric with
   ``errors="coerce"``. Strings sneak in from manual exports.

5. **Hash-based dedup** — duplicates are detected by content hash, not by
   ``drop_duplicates``. When the same ``budget_id`` appears twice with different
   content, the *policy* (declared here in code) is "keep the row with the most
   recent ``signed_at``". That is a business decision, not a technical one —
   documenting it is part of the deliverable.

The input is the **raw record**, not the rendered markdown; the orchestrator
passes ``Document.metadata.extra["raw"]``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

import pandas as pd

NULL_PLACEHOLDERS = {"TBD", "N/A", "n/a", "tbd", "", "null", "None", "-", "—"}

#: ISO-8601 dates are unambiguous and must NOT be read day-first (2024-05-02 is
#: 2 May, not 5 February). Everything else in this corpus is Spanish d/m/Y.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ].*)?$")

#: Columns where a disguised null is worth normalizing.
_NULLABLE_TEXT_COLUMNS = ("client_name", "contact", "contact_email", "notes")

#: Marker column added by the dedup step so the hash is auditable.
CONTENT_HASH_COLUMN = "content_hash"


def clean_budget_records(records: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """Return a cleaned DataFrame from the raw list of budget dicts."""
    df = pd.DataFrame(list(records))
    if df.empty or len(df.columns) == 0:
        # ``validate_with_policy`` handles the empty frame; keep the shape
        # callers expect (a DataFrame, not None).
        return df

    # 1. Null placeholders.
    for column in _NULLABLE_TEXT_COLUMNS:
        if column in df.columns:
            df[column] = df[column].map(
                lambda value: pd.NA
                if (isinstance(value, str) and value.strip() in NULL_PLACEHOLDERS)
                else value
            )

    # 2. Currency casing.
    if "currency" in df.columns:
        df["currency"] = df["currency"].map(
            lambda value: value.strip().upper() if isinstance(value, str) else value
        )

    # 3. Date coercion (permissive; ISO as ISO, everything else day-first).
    if "signed_at" in df.columns:
        # A column-wide ``to_datetime(dayfirst=True)`` is wrong twice over: it
        # infers one format from the first value (so a mixed ISO/d-m-Y column
        # silently becomes NaT), and it reads an ISO date as day-first. Parse
        # each value with the rule its shape calls for.
        df["signed_at"] = df["signed_at"].map(_parse_signed_at)

    # 4. Numeric coercion.
    if "total_amount" in df.columns:
        df["total_amount"] = pd.to_numeric(df["total_amount"], errors="coerce")

    # 5. Hash-based dedup with explicit business rule: keep latest signed_at.
    if {"budget_id", "signed_at"}.issubset(df.columns):
        df[CONTENT_HASH_COLUMN] = df.apply(_content_hash, axis=1)
        # Sort so the latest signed_at lands LAST per budget_id, then keep the
        # last. NaT sorts first, so a dated row beats an undated duplicate.
        df = df.sort_values(
            by=["budget_id", "signed_at"], na_position="first", kind="stable"
        )
        df = df.drop_duplicates(subset=["budget_id"], keep="last").reset_index(
            drop=True
        )

    return df


def _parse_signed_at(value: Any) -> Any:
    """Parse one ``signed_at`` value, returning ``NaT`` when it is not a date.

    ISO-shaped strings are parsed as ISO; anything else is parsed day-first,
    which is the Spanish convention the corpus uses (``12/03/2024``).
    """
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return pd.NaT
    if isinstance(value, pd.Timestamp):
        return value
    if not isinstance(value, str):
        return pd.to_datetime(value, errors="coerce")
    text = value.strip()
    if not text:
        return pd.NaT
    if _ISO_DATE_RE.match(text):
        return pd.to_datetime(text, errors="coerce", format="ISO8601")
    return pd.to_datetime(text, errors="coerce", dayfirst=True)


def _content_hash(row: pd.Series) -> str:
    """Canonical JSON of the row → sha256.

    ``NaN``/``NaT`` are normalized so two semantically identical rows that
    differ only in dtype hash the same.
    """
    payload = {
        str(key): _serializable(value)
        for key, value in row.items()
        if str(key) != CONTENT_HASH_COLUMN
    }
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _serializable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _serializable(v) for k, v in value.items()}
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    try:
        # ``pd.isna`` returns an array for array-likes; those were handled above.
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value
