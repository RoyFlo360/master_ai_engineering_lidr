"""PII analyzers: Presidio (Spanish model) behind a protocol, plus a fallback.

The default Presidio configuration ships with English spaCy models. On Hispanic
names ("Laura Fernández", "Javier Romero") it silently returns *zero* PERSON
entities — a failure mode much worse than a false positive because it goes
undetected. :func:`build_analyzer` is the single place that wires Presidio to
``es_core_news_md``.

Two hard constraints shape this module:

* **Tests must never load spaCy.** ``build_analyzer`` therefore checks that the
  model package is *installed* (``importlib.util.find_spec``, no import) before
  it imports spaCy at all, and raises :class:`PIIAnalyzerUnavailable` with an
  actionable message when it is not. Importing this module never imports
  Presidio or spaCy.

* **The pipeline must keep working without the model.** :class:`RegexFallbackAnalyzer`
  exposes the same ``analyze(text, language, entities)`` surface and covers the
  deterministic identifiers (email, ``BUDGET-YYYY-NNNN``, ``CLI-NNNN``). It
  cannot find person names — that is the honest limit, and it is why
  ``PII_ANALYZER_MODE=presidio`` exists for deployments that must fail closed.
"""
from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Sequence

from app.ingestion.pii.recognizers import (
    BUDGET_ID_ENTITY,
    BUDGET_ID_RE,
    CLIENT_CODE_ENTITY,
    CLIENT_CODE_RE,
)

AnalyzerMode = Literal["auto", "presidio", "regex"]

DEFAULT_LANGUAGE = "es"
EMAIL_ENTITY = "EMAIL_ADDRESS"

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


class PIIAnalyzerUnavailable(RuntimeError):
    """The configured Presidio/spaCy analyzer cannot be built on this host."""


@dataclass(frozen=True)
class PIISpan:
    """One detected entity. Mirrors the fields the pseudonymizer reads.

    Presidio's ``RecognizerResult`` is duck-type compatible (``entity_type``,
    ``start``, ``end``, ``score``), so the pseudonymizer accepts either.
    """

    entity_type: str
    start: int
    end: int
    score: float = 1.0


class RegexFallbackAnalyzer:
    """Deterministic, model-free analyzer with the Presidio surface.

    Detects: ``EMAIL_ADDRESS``, ``BUDGET_ID``, ``CLIENT_CODE``. Deliberately
    does *not* guess ``PERSON`` from capitalisation — a wrong pseudonym for a
    person is worse than none, and the honest statement "install the Spanish
    model to pseudonymize names" is the teaching point.
    """

    name = "regex_fallback"

    def analyze(
        self,
        text: str,
        language: str = DEFAULT_LANGUAGE,
        entities: Sequence[str] | None = None,
    ) -> list[PIISpan]:
        if not isinstance(text, str) or not text:
            return []
        wanted = set(entities) if entities else None
        spans: list[PIISpan] = []
        rules = (
            (EMAIL_ENTITY, _EMAIL_RE),
            (BUDGET_ID_ENTITY, BUDGET_ID_RE),
            (CLIENT_CODE_ENTITY, CLIENT_CODE_RE),
        )
        for entity_type, regex in rules:
            if wanted is not None and entity_type not in wanted:
                continue
            for match in regex.finditer(text):
                spans.append(
                    PIISpan(
                        entity_type=entity_type,
                        start=match.start(),
                        end=match.end(),
                        score=0.9,
                    )
                )
        return spans


def model_is_available(model_name: str) -> bool:
    """True when a spaCy model package of that name is installed.

    Uses ``find_spec`` only: no import, no model load, no 380 MB download.
    """
    if not model_name:
        return False
    try:
        return importlib.util.find_spec(model_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def build_analyzer(model_name: str | None = None) -> Any:
    """Return a Presidio ``AnalyzerEngine`` wired to the Spanish model.

    Raises:
        PIIAnalyzerUnavailable: the spaCy model is not installed, Presidio is
            missing, or the model fails to load.
    """
    if model_name is None:
        from app.config import get_settings

        model_name = get_settings().PRESIDIO_SPACY_MODEL
    if not model_is_available(model_name):
        raise PIIAnalyzerUnavailable(
            f"spaCy model {model_name!r} is not installed. Install it with "
            f"`python -m spacy download {model_name}` (or run the documented "
            "build step), or set PII_ANALYZER_MODE=regex to use the fallback "
            "analyzer (which cannot detect person names)."
        )
    return _cached_analyzer(model_name)


@lru_cache(maxsize=2)
def _cached_analyzer(model_name: str) -> Any:
    """Build (once per model) the Presidio engine with our recognizers."""
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        from app.ingestion.pii.recognizers import (
            BudgetIdRecognizer,
            ClientCodeRecognizer,
        )
    except ImportError as exc:  # pragma: no cover - presidio is a hard dependency
        raise PIIAnalyzerUnavailable(
            f"presidio-analyzer is not installed: {exc}"
        ) from exc

    try:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": DEFAULT_LANGUAGE, "model_name": model_name}],
            }
        )
        engine = AnalyzerEngine(
            nlp_engine=provider.create_engine(),
            supported_languages=[DEFAULT_LANGUAGE],
        )
    except Exception as exc:  # noqa: BLE001 - spaCy raises many shapes
        raise PIIAnalyzerUnavailable(
            f"could not load spaCy model {model_name!r}: {type(exc).__name__}: {exc}"
        ) from exc

    engine.registry.add_recognizer(BudgetIdRecognizer())
    engine.registry.add_recognizer(ClientCodeRecognizer())
    return engine


__all__ = [
    "AnalyzerMode",
    "DEFAULT_LANGUAGE",
    "EMAIL_ENTITY",
    "PIIAnalyzerUnavailable",
    "PIISpan",
    "RegexFallbackAnalyzer",
    "build_analyzer",
    "model_is_available",
]
