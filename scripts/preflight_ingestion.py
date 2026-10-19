#!/usr/bin/env python3
"""Pre-flight verification for the ingestion subsystem.

Run this BEFORE a demo or a deployment to catch the usual setup pitfalls:
missing packages, missing Spanish spaCy model, incomplete seed corpus, an
invalid catalog, an unmigrated database. Output is ✅ / ❌ per check; the exit
code is non-zero if anything *required* fails.

Optional checks (the Spanish model, ``/health``, the database schema) degrade to
a warning: the pipeline works without them — with documented limits — and a
preflight that refuses to run on a laptop would not get used.

Usage::

    .venv\\Scripts\\python.exe scripts/preflight_ingestion.py
    .venv\\Scripts\\python.exe scripts/preflight_ingestion.py --check-health
    docker compose exec api python scripts/preflight_ingestion.py
"""
from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
# Make ``app`` importable when this script runs directly from ``scripts/``.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ASCII markers on purpose: a Windows console runs cp1252 by default and a
# Unicode tick raises UnicodeEncodeError before the first check completes.
OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"


class Check:
    def __init__(
        self, name: str, fn: Callable[[], str | None], *, required: bool = True
    ) -> None:
        self.name = name
        self._fn = fn
        self.required = required

    def run(self) -> bool:
        try:
            detail = self._fn()
        except Exception as exc:  # noqa: BLE001 - a preflight reports, never raises
            mark = FAIL if self.required else WARN
            print(f"{mark}  {self.name}: {type(exc).__name__}: {exc}")
            return not self.required
        mark = OK if detail else OK
        print(f"{mark}  {self.name}" + (f": {detail}" if detail else ""))
        return True


# Package → minimum version we rely on. Version *ranges* are in pyproject.toml;
# this is the "is it installed at all, and recent enough to have the API we use"
# gate.
_MIN_VERSIONS = {
    "pandas": "2.2.0",
    "pandera": "0.20.0",
    "pyyaml": "6.0",
    "sqlalchemy": "2.0.0",
    "alembic": "1.13.0",
    "psycopg": "3.1.0",
    "faker": "24.0.0",
    "openpyxl": "3.1.0",
    "presidio-analyzer": "2.2.0",
    "presidio-anonymizer": "2.2.0",
    "spacy": "3.7.0",
}


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check_python_version() -> str:
    major, minor, *_ = sys.version_info
    if (major, minor) < (3, 11):
        raise RuntimeError(f"Python 3.11+ required, found {major}.{minor}")
    return f"{major}.{minor}.{sys.version_info.micro}"


def check_packages() -> str:
    missing: list[str] = []
    too_old: list[str] = []
    for package, minimum in _MIN_VERSIONS.items():
        try:
            installed = importlib_metadata.version(package)
        except importlib_metadata.PackageNotFoundError:
            missing.append(package)
            continue
        if _parse_version(installed) < _parse_version(minimum):
            too_old.append(f"{package} {installed} < {minimum}")
    if missing or too_old:
        raise RuntimeError("missing=" + ",".join(missing) + " too_old=" + ",".join(too_old))
    return f"{len(_MIN_VERSIONS)} packages OK"


def check_spacy_model() -> str:
    """Presence only — loading the model costs ~300 ms and a demo pays it once."""
    from app.config import get_settings
    from app.ingestion.pii.analyzer import model_is_available

    model_name = get_settings().PRESIDIO_SPACY_MODEL
    if not model_is_available(model_name):
        raise RuntimeError(
            f"{model_name} not installed — PII detection falls back to regex "
            f"(no person names). Install with: python -m spacy download {model_name}"
        )
    return f"{model_name} installed"


def check_seed_corpus() -> str:
    from app.config import get_settings

    root = Path(get_settings().INGESTION_DATA_ROOT)
    budgets = sorted((root / "budgets").glob("*.json"))
    transcripts = sorted((root / "transcripts").glob("*.txt"))
    xlsx = sorted(root.glob("*.xlsx"))
    if not budgets or not transcripts:
        raise RuntimeError(
            f"corpus incomplete under {root}: budgets={len(budgets)}, "
            f"transcripts={len(transcripts)}"
        )
    note = "" if xlsx else " (rate card XLSX absent — the `exclude` source is unused)"
    return f"{len(budgets)} json + {len(transcripts)} txt{note}"


def check_catalog() -> str:
    from app.config import get_settings
    from app.ingestion.catalog.loader import load_catalog

    settings = get_settings()
    catalog = load_catalog(settings.CATALOG_PATH)
    included = catalog.included_sources()
    return (
        f"version {catalog.version}, {len(catalog.sources)} sources, "
        f"{len(included)} included "
        f"({', '.join(source.name for source in included) or 'none'})"
    )


def check_database() -> str:
    """Connect and confirm the migration ran (the engine is lazy; this forces it)."""
    from sqlalchemy import inspect

    from app.persistence.database import get_engine

    tables = set(inspect(get_engine()).get_table_names())
    expected = {"pseudonym_mappings", "ingestion_jobs"}
    missing = expected - tables
    if missing:
        raise RuntimeError(f"missing tables {sorted(missing)} — run `alembic upgrade head`")
    return f"tables OK: {sorted(expected)}"


def check_health_endpoint(base_url: str) -> str:
    import urllib.request

    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
            return f"{response.status} OK"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"GET {url} failed — is the API running? ({exc})") from exc


def build_checks(base_url: str | None) -> list[Check]:
    checks = [
        Check("Python version", check_python_version),
        Check("Required packages", check_packages),
        Check("Seed corpus present", check_seed_corpus),
        Check("Catalog validates", check_catalog),
        Check("spaCy Spanish model", check_spacy_model, required=False),
        Check("Database schema", check_database, required=False),
    ]
    if base_url:
        checks.append(
            Check(
                "API /health",
                lambda: check_health_endpoint(base_url),
                required=False,
            )
        )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check-health",
        nargs="?",
        const="http://localhost:8000",
        default=None,
        metavar="BASE_URL",
        help="also probe <BASE_URL>/health (default http://localhost:8000)",
    )
    args = parser.parse_args(argv)

    print("Pre-flight — ingestion subsystem\n")
    failed = 0
    for check in build_checks(args.check_health):
        if not check.run() and check.required:
            failed += 1
    print()
    if failed:
        print(f"{FAIL}  {failed} required check(s) failed — fix before running")
        return 1
    print(f"{OK}  No required checks failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
