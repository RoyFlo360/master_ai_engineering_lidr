#!/usr/bin/env python3
"""Demo — cleaning + Pandera validation over the seed budgets.

Runnable form of the live demo (no REPL typing): loads every budget JSON from
the seed corpus, runs ``clean_budget_records`` then ``validate_with_policy``, and
prints the partition report. Expected story:

* 6 ficheros entran, 5 llegan a validación (el dedup colapsa BUDGET-2024-0005).
* ``total_amount: -50000`` se descarta; ``client_name: "TBD"`` va a cuarentena
  (recuperable: falta el nombre del cliente); el resto pasa.

Usage::

    .venv\\Scripts\\python.exe scripts/demo_cleaning.py
    docker compose exec api python scripts/demo_cleaning.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Make ``app`` importable when this script runs directly from ``scripts/``.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingestion.cleaning import (  # noqa: E402  (after the sys.path bootstrap)
    clean_budget_records,
    validate_with_policy,
)

BUDGETS_DIR = ROOT / "data" / "seed" / "budgets"


def main() -> int:
    paths = sorted(BUDGETS_DIR.glob("*.json"))
    if not paths:
        print(f"ERROR: no budget JSON files under {BUDGETS_DIR}")
        return 1

    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    print(f"Ficheros leídos: {len(records)}  ({BUDGETS_DIR})")

    df = clean_budget_records(records)
    print(f"Filas tras limpieza + dedup: {len(df)}")

    result = validate_with_policy(df)
    print("\nReport:")
    for key, value in result.report.items():
        if key == "routed":
            continue
        print(f"  {key}: {value}")

    if not result.discarded.empty:
        print("\nDescartadas (fallo fatal):")
        print(result.discarded[["budget_id", "total_amount"]].to_string(index=False))
    if not result.quarantined.empty:
        print("\nEn cuarentena (revisión humana):")
        print(result.quarantined[["budget_id", "client_name"]].to_string(index=False))
    if not result.valid.empty:
        print("\nVálidas:")
        print(result.valid[["budget_id", "client_name", "currency", "signed_at"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
