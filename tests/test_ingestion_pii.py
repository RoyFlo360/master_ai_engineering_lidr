"""PII: recognizer regexes, the pseudonymizer's guarantees, and the fallback.

No test here loads spaCy or Presidio's NLP engine: the pseudonymizer is driven
by a stub analyzer and the Presidio recognizer test only builds a
``PatternRecognizer`` (pure regex, no model).
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from app.ingestion.pii import (
    ConsistentPseudonymizer,
    InMemoryMappingStore,
    PIIAnalyzerUnavailable,
    PIISpan,
    RegexFallbackAnalyzer,
    build_analyzer,
    build_pseudonymizer,
    model_is_available,
)
from app.ingestion.pii.recognizers import (
    BUDGET_ID_RE,
    CLIENT_CODE_RE,
    BudgetIdRecognizer,
    ClientCodeRecognizer,
)

SALT = "test-salt"


class StubAnalyzer:
    """Returns fixed spans. Records the calls it received."""

    def __init__(self, spans: list[PIISpan] | None = None) -> None:
        self.spans = spans or []
        self.calls: list[dict] = []

    def analyze(self, text, language="es", entities=None):
        self.calls.append({"text": text, "language": language, "entities": entities})
        return list(self.spans)


def spans_for(text: str, *needles: tuple[str, str]) -> list[PIISpan]:
    """Build spans by locating each ``(needle, entity_type)`` in ``text``."""
    result = []
    for needle, entity_type in needles:
        start = text.index(needle)
        result.append(
            PIISpan(entity_type=entity_type, start=start, end=start + len(needle))
        )
    return result


def make_pseudonymizer(analyzer, store=None) -> ConsistentPseudonymizer:
    return ConsistentPseudonymizer(
        analyzer=analyzer,
        mapping_store=store if store is not None else InMemoryMappingStore(),
        salt=SALT,
        faker_locale="es_ES",
    )


# --------------------------------------------------------------------------- #
# Recognizer regexes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Presupuesto BUDGET-2024-0001 firmado",
        "BUDGET-1999-9999",
        "referencia BUDGET-2024-0001.",
    ],
)
def test_budget_id_regex_matches(text):
    assert BUDGET_ID_RE.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "BUDGET-2024-1",          # three-digit serial
        "BUDGET-24-0001",         # two-digit year
        "XBUDGET-2024-0001",      # left boundary missing (no word boundary)
        "budget-2024-0001",       # lowercase
    ],
)
def test_budget_id_regex_does_not_match(text):
    assert not BUDGET_ID_RE.search(text)


@pytest.mark.parametrize("text", ["CLI-0042", "cliente CLI-9999."])
def test_client_code_regex_matches(text):
    assert CLIENT_CODE_RE.search(text)


@pytest.mark.parametrize("text", ["CLI-42", "CLI-00042", "cli-0042", "XCLI-0042"])
def test_client_code_regex_does_not_match(text):
    assert not CLIENT_CODE_RE.search(text)


def test_presidio_recognizers_are_wired_to_the_same_patterns():
    pytest.importorskip("presidio_analyzer")
    for recognizer_factory, entity in (
        (BudgetIdRecognizer, "BUDGET_ID"),
        (ClientCodeRecognizer, "CLIENT_CODE"),
    ):
        recognizer = recognizer_factory()
        assert entity in recognizer.supported_entities
        try:
            results = recognizer.analyze(
                text="Presupuesto BUDGET-2024-0001 del cliente CLI-0042.",
                entities=[entity],
            )
        except TypeError:  # pragma: no cover - presidio API drift, not our code
            pytest.skip("presidio PatternRecognizer.analyze signature differs")
        assert [result.entity_type for result in results] == [entity]


# --------------------------------------------------------------------------- #
# ConsistentPseudonymizer
# --------------------------------------------------------------------------- #
def test_entities_are_replaced_and_text_is_rebuilt():
    text = "Laura Fernández firma BUDGET-2024-0001 hoy."
    analyzer = StubAnalyzer(
        spans_for(text, ("Laura Fernández", "PERSON"), ("BUDGET-2024-0001", "BUDGET_ID"))
    )
    result = make_pseudonymizer(analyzer).pseudonymize(text)

    assert "Laura Fernández" not in result.pseudonymized_text
    assert "BUDGET-2024-0001" not in result.pseudonymized_text
    assert len(result.applied) == 2
    assert result.pseudonymized_text.endswith(" hoy.")
    for mapping in result.applied:
        assert mapping.pseudonym in result.pseudonymized_text


def test_replacement_is_applied_right_to_left():
    text = "A BUDGET-2024-0001 y CLI-0042."
    analyzer = StubAnalyzer(
        spans_for(text, ("BUDGET-2024-0001", "BUDGET_ID"), ("CLI-0042", "CLIENT_CODE"))
    )
    result = make_pseudonymizer(analyzer).pseudonymize(text)

    # Spans are applied from the end of the string backwards, so the trailing
    # entity carries the larger offset.
    assert result.applied[0].start > result.applied[1].start
    budget = next(m for m in result.applied if m.entity_type == "BUDGET_ID")
    client = next(m for m in result.applied if m.entity_type == "CLIENT_CODE")
    assert result.pseudonymized_text == f"A {budget.pseudonym} y {client.pseudonym}."


def test_same_value_gets_the_same_pseudonym_across_calls():
    text = "Habla con Laura Fernández. Laura Fernández firma el presupuesto."
    analyzer = StubAnalyzer(
        [
            PIISpan(
                "PERSON",
                text.index("Laura Fernández"),
                text.index("Laura Fernández") + len("Laura Fernández"),
            ),
            PIISpan(
                "PERSON",
                text.rindex("Laura Fernández"),
                text.rindex("Laura Fernández") + len("Laura Fernández"),
            ),
        ]
    )
    pseudonymizer = make_pseudonymizer(analyzer)

    first = pseudonymizer.pseudonymize(text)
    second = pseudonymizer.pseudonymize(text)

    assert first.pseudonymized_text == second.pseudonymized_text
    pseudonyms = {mapping.pseudonym for mapping in first.applied}
    assert len(pseudonyms) == 1


def test_distinct_values_get_distinct_pseudonyms():
    text = "Laura Fernández y Diego Marín."
    analyzer = StubAnalyzer(
        spans_for(text, ("Laura Fernández", "PERSON"), ("Diego Marín", "PERSON"))
    )
    result = make_pseudonymizer(analyzer).pseudonymize(text)

    pseudonyms = {mapping.pseudonym for mapping in result.applied}
    assert len(pseudonyms) == 2


def test_text_without_pii_is_left_untouched():
    text = "Reunión de seguimiento sin datos personales."
    result = make_pseudonymizer(StubAnalyzer([])).pseudonymize(text)

    assert result.pseudonymized_text == text
    assert result.applied == []


def test_pseudonymizer_passes_the_language_and_the_entity_filter():
    analyzer = StubAnalyzer([])
    pseudonymizer = make_pseudonymizer(analyzer)

    pseudonymizer.pseudonymize("texto", entities=["PERSON"])

    assert analyzer.calls[0]["language"] == "es"
    assert analyzer.calls[0]["entities"] == ["PERSON"]


def test_the_mapping_store_is_keyed_by_hmac_not_by_the_value():
    text = "Laura Fernández"
    analyzer = StubAnalyzer(spans_for(text, ("Laura Fernández", "PERSON")))
    store = InMemoryMappingStore()
    result = make_pseudonymizer(analyzer, store).pseudonymize(text)

    expected = hmac.new(SALT.encode(), text.encode("utf-8"), hashlib.sha256).hexdigest()
    assert result.applied[0].original_hash == expected
    assert [key for _, key in store.keys()] == [expected]
    assert "Laura Fernández" not in str(store.keys())
    assert len(expected) == 64


def test_forget_removes_the_mapping():
    store = InMemoryMappingStore()
    text = "Laura Fernández"
    analyzer = StubAnalyzer(spans_for(text, ("Laura Fernández", "PERSON")))
    pseudonymizer = make_pseudonymizer(analyzer, store)
    result = pseudonymizer.pseudonymize(text)
    original_hash = result.applied[0].original_hash

    assert len(store) == 1
    assert store.forget("PERSON", original_hash) is True
    assert len(store) == 0
    assert store.forget("PERSON", original_hash) is False


def test_empty_text_is_a_no_op():
    result = make_pseudonymizer(StubAnalyzer([])).pseudonymize("")

    assert result.pseudonymized_text == ""
    assert result.applied == []


def test_overlapping_spans_do_not_corrupt_the_text():
    text = "Contacto laura.fernandez@acme.es"
    email_start = text.index("laura.fernandez@acme.es")
    analyzer = StubAnalyzer(
        [
            PIISpan("EMAIL_ADDRESS", email_start, len(text)),
            PIISpan("PERSON", email_start, email_start + 5),  # nested inside the email
        ]
    )
    result = make_pseudonymizer(analyzer).pseudonymize(text)

    assert "laura.fernandez@acme.es" not in result.pseudonymized_text
    assert len(result.applied) == 1


def test_a_missing_salt_is_refused():
    with pytest.raises(ValueError, match="salt"):
        ConsistentPseudonymizer(
            analyzer=StubAnalyzer([]), mapping_store=InMemoryMappingStore(), salt=""
        )


# --------------------------------------------------------------------------- #
# Analyzer selection and the regex fallback
# --------------------------------------------------------------------------- #
class FakeSettings:
    PII_ANALYZER_MODE = "regex"
    PRESIDIO_SPACY_MODEL = "es_core_news_md"
    PSEUDONYM_HASH_SALT = SALT
    PSEUDONYM_FAKER_LOCALE = "es_ES"


def test_regex_fallback_finds_the_deterministic_identifiers():
    analyzer = RegexFallbackAnalyzer()
    text = "Presupuesto BUDGET-2024-0001, cliente CLI-0042, correo laura@acme.es"

    entities = {span.entity_type: text[span.start : span.end] for span in analyzer.analyze(text)}

    assert entities["BUDGET_ID"] == "BUDGET-2024-0001"
    assert entities["CLIENT_CODE"] == "CLI-0042"
    assert entities["EMAIL_ADDRESS"] == "laura@acme.es"


def test_regex_fallback_honours_the_entity_filter():
    analyzer = RegexFallbackAnalyzer()
    text = "BUDGET-2024-0001 y laura@acme.es"

    spans = analyzer.analyze(text, entities=["EMAIL_ADDRESS"])

    assert [span.entity_type for span in spans] == ["EMAIL_ADDRESS"]


def test_regex_fallback_does_not_invent_person_names():
    analyzer = RegexFallbackAnalyzer()

    assert analyzer.analyze("Laura Fernández firma el contrato.") == []


def test_build_pseudonymizer_with_regex_mode_pseudonymizes_identifiers():
    text = "Presupuesto BUDGET-2024-0001 del cliente CLI-0042."
    pseudonymizer = build_pseudonymizer(FakeSettings(), InMemoryMappingStore())

    result = pseudonymizer.pseudonymize(text)

    assert "BUDGET-2024-0001" not in result.pseudonymized_text
    assert "CLI-0042" not in result.pseudonymized_text
    assert len(result.applied) == 2


def test_build_analyzer_raises_a_clear_error_when_the_model_is_missing():
    with pytest.raises(PIIAnalyzerUnavailable, match="not installed"):
        build_analyzer("es_core_news_modelo_que_no_existe_xyz")


def test_build_pseudonymizer_in_presidio_mode_fails_closed():
    class PresidioSettings(FakeSettings):
        PII_ANALYZER_MODE = "presidio"
        PRESIDIO_SPACY_MODEL = "es_core_news_modelo_que_no_existe_xyz"

    with pytest.raises(PIIAnalyzerUnavailable):
        build_pseudonymizer(PresidioSettings(), InMemoryMappingStore())


# --------------------------------------------------------------------------- #
# Overlapping spans (regression)
# --------------------------------------------------------------------------- #
def test_a_specific_recognizer_beats_a_person_tag_on_the_same_span():
    """Presidio's Spanish NER also tags ``CLI-0078`` as PERSON (score 0.85).

    The client-code recognizer scores 0.95 on the same span. Selection must be
    deterministic and must prefer the specific recognizer: if PERSON won, an
    identifier would be replaced by a human name and traceability would break.
    The old ``(start, -end)`` sort made the winner depend on the analyzer's
    output order, which is exactly the kind of thing that changes silently.
    """
    text = "El cliente CLI-0078 firmó."
    start = text.index("CLI-0078")
    end = start + len("CLI-0078")
    analyzer = StubAnalyzer(
        [
            PIISpan("PERSON", start, end, score=0.85),
            PIISpan("CLIENT_CODE", start, end, score=0.95),
        ]
    )

    result = make_pseudonymizer(analyzer).pseudonymize(text)

    assert [mapping.entity_type for mapping in result.applied] == ["CLIENT_CODE"]
    assert result.applied[0].pseudonym.startswith("CLI-")


def test_higher_confidence_wins_regardless_of_input_order():
    text = "Cliente CLI-0042."
    start = text.index("CLI-0042")
    end = start + len("CLI-0042")
    spans = [
        PIISpan("PERSON", start, end, score=0.85),
        PIISpan("CLIENT_CODE", start, end, score=0.95),
    ]

    forwards = make_pseudonymizer(StubAnalyzer(list(spans))).pseudonymize(text)
    backwards = make_pseudonymizer(StubAnalyzer(list(reversed(spans)))).pseudonymize(text)

    assert forwards.applied[0].entity_type == "CLIENT_CODE"
    assert backwards.applied[0].entity_type == "CLIENT_CODE"
    assert forwards.pseudonymized_text == backwards.pseudonymized_text


def test_an_inner_span_does_not_split_an_outer_email():
    """``URL`` overlaps ``EMAIL_ADDRESS``; the email must be redacted whole."""
    text = "Escribe a laura@acme.es hoy."
    email_start = text.index("laura@acme.es")
    email_end = email_start + len("laura@acme.es")
    url_start = text.index("acme.es")
    analyzer = StubAnalyzer(
        [
            PIISpan("EMAIL_ADDRESS", email_start, email_end, score=1.0),
            PIISpan("URL", url_start, url_start + len("acme.es"), score=0.5),
        ]
    )

    result = make_pseudonymizer(analyzer).pseudonymize(text)

    assert "laura@acme.es" not in result.pseudonymized_text
    assert "acme.es" not in result.pseudonymized_text
    assert [mapping.entity_type for mapping in result.applied] == ["EMAIL_ADDRESS"]


# --------------------------------------------------------------------------- #
# The real Spanish model (opt-in)
# --------------------------------------------------------------------------- #
SPANISH_MODEL = "es_core_news_md"


@pytest.mark.skipif(
    not model_is_available(SPANISH_MODEL),
    reason="Spanish spaCy model not installed; the regex fallback covers this run",
)
def test_the_spanish_model_detects_hispanic_person_names():
    """The whole reason the model exists: English NER misses these names."""
    analyzer = build_analyzer(SPANISH_MODEL)
    text = "Laura Fernández y Javier Romero revisaron el presupuesto."

    spans = analyzer.analyze(text=text, language="es")
    people = {text[s.start : s.end] for s in spans if s.entity_type == "PERSON"}

    assert "Laura Fernández" in people
    assert "Javier Romero" in people


# --------------------------------------------------------------------------- #
# The entity allow-list (over-redaction is a data-loss bug)
# --------------------------------------------------------------------------- #
def test_default_entity_list_is_passed_when_none_is_given():
    analyzer = StubAnalyzer([])
    make_pseudonymizer(analyzer).pseudonymize("texto")

    from app.ingestion.pii.pseudonymizer import DEFAULT_ENTITIES

    assert analyzer.calls[0]["entities"] == list(DEFAULT_ENTITIES)


def test_the_allow_list_excludes_dates_and_urls():
    """A signed date is corpus data, not personal data."""
    from app.ingestion.pii.pseudonymizer import DEFAULT_ENTITIES

    assert "PERSON" in DEFAULT_ENTITIES
    assert "EMAIL_ADDRESS" in DEFAULT_ENTITIES
    assert "DATE_TIME" not in DEFAULT_ENTITIES
    assert "URL" not in DEFAULT_ENTITIES


def test_a_date_span_is_not_redacted_even_if_the_analyzer_returns_it():
    """Presidio's Spanish model emits DATE_TIME 0.60; we must not honour it."""
    text = "Firmado el 2024-02-14 por Laura Fernández."
    date_start = text.index("2024-02-14")
    person_start = text.index("Laura Fernández")
    analyzer = StubAnalyzer(
        [
            PIISpan("DATE_TIME", date_start, date_start + len("2024-02-14"), score=0.6),
            PIISpan(
                "PERSON",
                person_start,
                person_start + len("Laura Fernández"),
                score=0.85,
            ),
        ]
    )

    result = make_pseudonymizer(analyzer).pseudonymize(text)

    assert "2024-02-14" in result.pseudonymized_text
    assert "Laura Fernández" not in result.pseudonymized_text
    assert [mapping.entity_type for mapping in result.applied] == ["PERSON"]
