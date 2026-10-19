"""Startup validation of the source catalog (spec 011, Q17).

A malformed catalog is a configuration bug, not a runtime condition. The
reference implementation failed *open*: the app booted and every ingestion
request 404'd with no clue why. The policy here:

* ``APP_ENV=production`` → raise. A service that cannot say which sources it
  trusts must not accept traffic.
* anything else → log an error and continue with ``None``. Local development and
  CI should not be blocked by a YAML typo in an unrelated file.

The router's ``Depends(get_catalog)`` still raises on first use, so the failure
is never silent — it is only delayed.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.ingestion.catalog.loader import load_catalog
from app.ingestion.catalog.models import DataCatalog

log = structlog.get_logger(__name__)


def validate_catalog_at_startup(settings: Any) -> DataCatalog | None:
    """Load the catalog, or abort in production when it does not validate."""
    path = getattr(settings, "CATALOG_PATH", None)
    environment = getattr(settings, "APP_ENV", "development")

    try:
        catalog = load_catalog(path)
    except Exception as exc:  # noqa: BLE001 - any failure to load is the same bug
        log.error(
            "catalog_invalid",
            path=str(path),
            environment=environment,
            error_type=type(exc).__name__,
            error=str(exc)[:400],
        )
        if environment == "production":
            raise RuntimeError(
                f"refusing to start: catalog {str(path)!r} is invalid "
                f"({type(exc).__name__})"
            ) from exc
        return None

    log.info(
        "catalog_loaded",
        version=catalog.version,
        sources=len(catalog.sources),
        included=len(catalog.included_sources()),
    )
    return catalog


__all__ = ["validate_catalog_at_startup"]
