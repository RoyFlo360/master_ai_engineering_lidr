"""FastAPI dependency factories for the ingestion subsystem.

These live here rather than in ``app/dependencies.py`` because that module is
owned elsewhere in this change; the router imports from here.

Every factory is ``lru_cache``d and lazy:

* ``get_catalog`` reads and validates the YAML exactly once per process — the
  catalog is immutable at runtime and re-reading it per request would turn a
  config file into a hot path.
* ``get_filesystem_loader`` / ``get_parser_registry`` are stateless singletons.
* ``get_pseudonymizer_factory`` returns a *factory*, not a pseudonymizer: the
  mapping store is bound to the session of whoever runs the ingestion (a
  BackgroundTask owns its own session), and the analyzer — Presidio or the regex
  fallback — is only built when the first run actually needs it. Importing this
  module therefore never loads spaCy.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.ingestion.catalog.loader import load_catalog
from app.ingestion.catalog.models import DataCatalog
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.parsers.registry import ParserRegistry, default_registry
from app.ingestion.pii import PseudonymizerFactory, make_pseudonymizer_factory


@lru_cache
def get_catalog() -> DataCatalog:
    """Load and validate the catalog at ``Settings.CATALOG_PATH`` (once)."""
    settings = get_settings()
    return load_catalog(settings.CATALOG_PATH)


@lru_cache
def get_filesystem_loader() -> FileSystemLoader:
    """Loader rooted at ``Settings.INGESTION_DATA_ROOT`` (once)."""
    settings = get_settings()
    return FileSystemLoader(settings.INGESTION_DATA_ROOT)


@lru_cache
def get_parser_registry() -> ParserRegistry:
    """Registry with only ``json`` and ``txt`` parsers registered (once)."""
    return default_registry()


@lru_cache
def get_pseudonymizer_factory() -> PseudonymizerFactory:
    """``store -> pseudonymizer`` factory bound to the current settings (once)."""
    return make_pseudonymizer_factory(get_settings())


def clear_ingestion_caches() -> None:
    """Drop the cached singletons. Tests and ``reload``-style scripts use this."""
    get_catalog.cache_clear()
    get_filesystem_loader.cache_clear()
    get_parser_registry.cache_clear()
    get_pseudonymizer_factory.cache_clear()


__all__ = [
    "clear_ingestion_caches",
    "get_catalog",
    "get_filesystem_loader",
    "get_parser_registry",
    "get_pseudonymizer_factory",
]
