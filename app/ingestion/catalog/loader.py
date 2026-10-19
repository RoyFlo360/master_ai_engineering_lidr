"""Load and validate the YAML catalog.

Reads a YAML file with ``yaml.safe_load``, validates it against
:class:`DataCatalog`. The loader itself is intentionally trivial — all the heavy
lifting (decision rules, duplicate detection) lives in the model, not here.

``safe_load`` is a security boundary, not a style choice: a catalog is a file
someone else may edit, and ``yaml.load`` with the full loader executes
``!!python/object`` tags. ``tests/test_ingestion_security.py`` pins that.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from app.ingestion.catalog.models import DataCatalog


def load_catalog(path: str | Path) -> DataCatalog:
    """Parse ``path`` as YAML and validate as a :class:`DataCatalog`.

    Raises:
        FileNotFoundError: the path does not exist.
        ValueError: the YAML is not a mapping, or the catalog fails validation
            (Pydantic wraps its own errors as ``ValidationError``, a ValueError).
        yaml.YAMLError: the YAML is malformed or carries an unsafe tag.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"catalog at {str(path)!r} must be a YAML mapping, got {type(raw).__name__}"
        )
    return DataCatalog.model_validate(raw)


def _main(argv: list[str]) -> int:
    """CLI: ``python -m app.ingestion.catalog.loader data/catalog/catalog.yaml``."""
    if len(argv) != 2:
        print("usage: python -m app.ingestion.catalog.loader <path.yaml>")
        return 1
    catalog = load_catalog(argv[1])
    included = catalog.included_sources()
    print(f"Catalog version: {catalog.version}")
    print(f"Sources total:  {len(catalog.sources)}")
    print(f"Sources included: {len(included)}")
    for src in catalog.sources:
        print(f"  - {src.name} ({src.format}) decision={src.decision.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
