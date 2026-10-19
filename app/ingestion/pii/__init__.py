"""GDPR-compliant pseudonymization layer.

Four concerns, four modules:

* ``analyzer`` — builds Presidio's ``AnalyzerEngine`` against
  ``es_core_news_md`` (and raises a clear error when it is absent), plus the
  model-free regex fallback used when the model is not installed.
* ``recognizers`` — the ``BUDGET-YYYY-NNNN`` / ``CLI-NNNN`` patterns, as regexes
  and as Presidio :class:`PatternRecognizer`s.
* ``pseudonymizer`` — :class:`ConsistentPseudonymizer`. Same original value →
  same pseudonym, across documents and runs, backed by a persistent
  :class:`MappingStore` so the operation is reversible (Art. 17).
* ``mapping_store`` — in-memory and database-backed stores.

:func:`build_pseudonymizer` is the composition root: Presidio when the mode
allows it and the model loads, the regex fallback otherwise. Tests and the demo
script use it with an :class:`InMemoryMappingStore`, so they never touch a model.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from app.ingestion.pii.analyzer import (
    DEFAULT_LANGUAGE,
    PIIAnalyzerUnavailable,
    PIISpan,
    RegexFallbackAnalyzer,
    build_analyzer,
    model_is_available,
)
from app.ingestion.pii.mapping_store import (
    DatabaseMappingStore,
    InMemoryMappingStore,
    MappingStore,
    PostgresMappingStore,
    PseudonymFactory,
)
from app.ingestion.pii.pseudonymizer import (
    AppliedMapping,
    ConsistentPseudonymizer,
    PseudonymizationResult,
)

#: A factory that builds a pseudonymizer bound to a mapping store.
PseudonymizerFactory = Callable[[MappingStore | None], ConsistentPseudonymizer]


def build_pseudonymizer(
    settings: Any | None = None,
    store: MappingStore | None = None,
    *,
    mode: str | None = None,
) -> ConsistentPseudonymizer:
    """Build the pseudonymizer for the configured analyzer mode.

    ``auto`` (default) uses Presidio when it can be built and degrades to the
    regex analyzer with a warning; ``presidio`` raises instead (fail closed);
    ``regex`` forces the fallback.
    """
    settings = settings or _settings()
    resolved_mode = mode or getattr(settings, "PII_ANALYZER_MODE", "auto")
    analyzer = _select_analyzer(
        mode=resolved_mode,
        model_name=getattr(settings, "PRESIDIO_SPACY_MODEL", ""),
    )
    return ConsistentPseudonymizer(
        analyzer=analyzer,
        mapping_store=store if store is not None else InMemoryMappingStore(),
        salt=getattr(settings, "PSEUDONYM_HASH_SALT", ""),
        faker_locale=getattr(settings, "PSEUDONYM_FAKER_LOCALE", "es_ES"),
    )


def make_pseudonymizer_factory(settings: Any | None = None) -> PseudonymizerFactory:
    """Return a ``store -> pseudonymizer`` factory bound to ``settings``."""

    def factory(store: MappingStore | None = None) -> ConsistentPseudonymizer:
        return build_pseudonymizer(settings, store)

    return factory


def _select_analyzer(*, mode: str, model_name: str) -> Any:
    if mode not in {"auto", "presidio", "regex"}:
        raise ValueError(f"unknown PII_ANALYZER_MODE {mode!r}")
    if mode == "regex":
        return RegexFallbackAnalyzer()
    try:
        return build_analyzer(model_name)
    except PIIAnalyzerUnavailable:
        if mode == "presidio":
            raise
        return RegexFallbackAnalyzer()


@lru_cache(maxsize=4)
def _settings() -> Any:
    from app.config import get_settings

    return get_settings()


def __getattr__(name: str) -> Any:
    """Expose the Presidio recognizers lazily (PEP 562).

    ``from app.ingestion.pii import BudgetIdRecognizer`` works, but plain
    ``import app.ingestion.pii`` does not import Presidio.
    """
    if name in {"BudgetIdRecognizer", "ClientCodeRecognizer"}:
        from app.ingestion.pii import recognizers

        return getattr(recognizers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AppliedMapping",
    "BudgetIdRecognizer",
    "ClientCodeRecognizer",
    "ConsistentPseudonymizer",
    "DEFAULT_LANGUAGE",
    "DatabaseMappingStore",
    "InMemoryMappingStore",
    "MappingStore",
    "PIIAnalyzerUnavailable",
    "PIISpan",
    "PostgresMappingStore",
    "PseudonymFactory",
    "PseudonymizationResult",
    "PseudonymizerFactory",
    "RegexFallbackAnalyzer",
    "build_analyzer",
    "build_pseudonymizer",
    "make_pseudonymizer_factory",
    "model_is_available",
]
