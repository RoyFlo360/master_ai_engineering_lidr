"""Domain-specific recognizers and the regexes they are built on.

Presidio detects PERSON, EMAIL, LOCATION, etc. out of the box, but the Proyecto
2 corpus also carries:

* **BUDGET_ID** — ``BUDGET-YYYY-NNNN`` (year + four-digit serial).
* **CLIENT_CODE** — ``CLI-NNNN`` (four-digit serial).

Both are deterministic patterns with a clear shape — high-precision regex
matches that land with a confidence score close to 1.0.

The regexes are module-level constants so the *pattern* can be tested, and used
by the regex fallback analyzer, without importing Presidio. The Presidio
recognizer classes are thin wrappers over them and are imported lazily by
``app.ingestion.pii`` (PEP 562 ``__getattr__``) to keep ``import
app.ingestion.pii`` cheap.
"""
from __future__ import annotations

import re
from typing import ClassVar

BUDGET_ID_ENTITY = "BUDGET_ID"
CLIENT_CODE_ENTITY = "CLIENT_CODE"

BUDGET_ID_PATTERN = r"\bBUDGET-\d{4}-\d{4}\b"
CLIENT_CODE_PATTERN = r"\bCLI-\d{4}\b"

BUDGET_ID_RE = re.compile(BUDGET_ID_PATTERN)
CLIENT_CODE_RE = re.compile(CLIENT_CODE_PATTERN)

#: Regex → recognizer confidence. Both patterns are unambiguous, so 0.95.
PATTERN_SCORE = 0.95


class BudgetIdRecognizer:
    """Presidio recognizer for ``BUDGET-YYYY-NNNN`` budget identifiers.

    Instantiation imports Presidio (not spaCy — no model is loaded here). The
    class is a factory in disguise on purpose: subclassing ``PatternRecognizer``
    at import time would make ``import app.ingestion.pii.recognizers`` depend on
    Presidio even for callers that only want the regex.
    """

    supported_entity: ClassVar[str] = BUDGET_ID_ENTITY

    def __new__(cls):  # noqa: D102 - delegating constructor
        return _pattern_recognizer(
            entity=BUDGET_ID_ENTITY,
            name="BudgetIdRecognizer",
            pattern_name="budget_id_pattern",
            pattern=BUDGET_ID_PATTERN,
            score=PATTERN_SCORE,
        )


class ClientCodeRecognizer:
    """Presidio recognizer for ``CLI-NNNN`` client codes."""

    supported_entity: ClassVar[str] = CLIENT_CODE_ENTITY

    def __new__(cls):  # noqa: D102 - delegating constructor
        return _pattern_recognizer(
            entity=CLIENT_CODE_ENTITY,
            name="ClientCodeRecognizer",
            pattern_name="client_code_pattern",
            pattern=CLIENT_CODE_PATTERN,
            score=PATTERN_SCORE,
        )


def _pattern_recognizer(
    *, entity: str, name: str, pattern_name: str, pattern: str, score: float
):
    """Build a Presidio ``PatternRecognizer`` bound to Spanish."""
    from presidio_analyzer import Pattern, PatternRecognizer

    return PatternRecognizer(
        supported_entity=entity,
        name=name,
        patterns=[Pattern(name=pattern_name, regex=pattern, score=score)],
        supported_language="es",
    )


def iter_regex_matches(text: str):
    """Yield ``(entity_type, start, end)`` for every domain identifier in ``text``."""
    for entity, regex in ((BUDGET_ID_ENTITY, BUDGET_ID_RE), (CLIENT_CODE_ENTITY, CLIENT_CODE_RE)):
        for match in regex.finditer(text):
            yield entity, match.start(), match.end()


__all__ = [
    "BUDGET_ID_ENTITY",
    "BUDGET_ID_PATTERN",
    "BUDGET_ID_RE",
    "BudgetIdRecognizer",
    "CLIENT_CODE_ENTITY",
    "CLIENT_CODE_PATTERN",
    "CLIENT_CODE_RE",
    "ClientCodeRecognizer",
    "PATTERN_SCORE",
    "iter_regex_matches",
]
