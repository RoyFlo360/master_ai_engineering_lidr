"""Tests for optional pseudonymization on the ingest path.

These are the tests that matter for this feature, so they assert the *properties*
rather than the plumbing:

* with the flag on, the personal data is **gone** from what is stored;
* with the flag off, the text is unchanged (so the default is not silently
  costing anyone distances);
* the same value maps to the same pseudonym across requests (the "consistent" in
  ConsistentPseudonymizer — without this the feature is a randomizer);
* factual content (dates, amounts, hours) survives, because over-redaction is
  data loss dressed as privacy;
* traceability survives — ``chunk_id`` and metadata are untouched;
* a failure aborts the ingest instead of storing plaintext.

Everything here is hermetic: a stub analyzer stands in for Presidio, so no spaCy
model is loaded and no network is touched. The real Presidio path is exercised by
``tests/test_ingestion_pii.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.embedding_pipeline.chunking.base import count_tokens
from app.embedding_pipeline.ingest_service import RagIngestService
from app.embedding_pipeline.pseudonymize import (
    PseudonymizationFailed,
    pseudonymize_chunks,
)
from app.embedding_pipeline.schemas import Budget, Chunk
from app.ingestion.pii import (
    ConsistentPseudonymizer,
    InMemoryMappingStore,
)
from app.ingestion.pii.analyzer import PIISpan

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "budgets_sample.json"


# --- a stub analyzer: finds fixed substrings, like Presidio would ------------


class SubstringAnalyzer:
    """Reports a span for every occurrence of a configured literal.

    Presidio's contract is ``analyze(text, language, entities) -> [span]`` with
    ``entity_type``/``start``/``end``/``score``. This stub keeps that surface so
    the real ``ConsistentPseudonymizer`` runs unmodified.
    """

    name = "substring_stub"

    def __init__(self, needles: dict[str, str]) -> None:
        self._needles = needles

    def analyze(self, text: str, language: str = "es", entities=None) -> list[PIISpan]:
        spans: list[PIISpan] = []
        for value, entity_type in self._needles.items():
            start = text.find(value)
            while start != -1:
                spans.append(
                    PIISpan(
                        entity_type=entity_type,
                        start=start,
                        end=start + len(value),
                        score=0.99,
                    )
                )
                start = text.find(value, start + 1)
        return spans


def build_pseudonymizer(needles: dict[str, str], salt: str = "test-salt") -> ConsistentPseudonymizer:
    return ConsistentPseudonymizer(
        analyzer=SubstringAnalyzer(needles),
        mapping_store=InMemoryMappingStore(),
        salt=salt,
        faker_locale="es_ES",
    )


def chunk(text: str, chunk_id: str = "BUD-1::C-1") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        metadata={"budget_id": "BUD-1", "component_id": "C-1", "year": 2024},
        token_count=count_tokens(text),
    )


PII_TEXT = (
    "[Project: Banking API for Laura Fernández]\n"
    "Component: OAuth 2.0 backend\n"
    "Contact: laura.fernandez@acmebank.es\n"
    "Signed 2024-02-14 for a total of 48,000 EUR over 120 hours."
)


# --- the wrapper itself ------------------------------------------------------


def test_personal_data_is_removed_from_the_chunk_text() -> None:
    pseudonymizer = build_pseudonymizer(
        {"Laura Fernández": "PERSON", "laura.fernandez@acmebank.es": "EMAIL_ADDRESS"}
    )

    result, replaced = pseudonymize_chunks([chunk(PII_TEXT)], pseudonymizer)

    text = result[0].text
    assert "Laura Fernández" not in text
    assert "laura.fernandez@acmebank.es" not in text
    assert replaced == 2


def test_factual_content_survives_pseudonymization() -> None:
    """Over-redaction is data loss. Dates, amounts and hours must stay."""
    pseudonymizer = build_pseudonymizer(
        {"Laura Fernández": "PERSON", "laura.fernandez@acmebank.es": "EMAIL_ADDRESS"}
    )

    result, _ = pseudonymize_chunks([chunk(PII_TEXT)], pseudonymizer)

    text = result[0].text
    assert "2024-02-14" in text
    assert "48,000 EUR" in text
    assert "120 hours" in text
    assert "OAuth 2.0 backend" in text


def test_token_count_is_recomputed_when_the_text_changes() -> None:
    """A stale count would misreport batch size and cost."""
    original = chunk(PII_TEXT + " " + "padding " * 30)
    pseudonymizer = build_pseudonymizer({"Laura Fernández": "PERSON"})

    result, _ = pseudonymize_chunks([original], pseudonymizer)

    assert result[0].text != original.text
    assert result[0].token_count == count_tokens(result[0].text)
    assert result[0].token_count != original.token_count


def test_traceability_is_preserved() -> None:
    """chunk_id and metadata are what make a hit citable; they must not move."""
    original = chunk(PII_TEXT)
    pseudonymizer = build_pseudonymizer({"Laura Fernández": "PERSON"})

    result, _ = pseudonymize_chunks([original], pseudonymizer)

    assert result[0].chunk_id == original.chunk_id
    assert result[0].metadata == original.metadata


def test_the_input_chunks_are_not_mutated() -> None:
    original = chunk(PII_TEXT)
    pseudonymizer = build_pseudonymizer({"Laura Fernández": "PERSON"})

    pseudonymize_chunks([original], pseudonymizer)

    assert "Laura Fernández" in original.text


def test_a_clean_chunk_is_returned_untouched() -> None:
    original = chunk("Component: OAuth 2.0 backend\nEstimated hours: 120")
    pseudonymizer = build_pseudonymizer({"Laura Fernández": "PERSON"})

    result, replaced = pseudonymize_chunks([original], pseudonymizer)

    assert replaced == 0
    assert result[0] is original


def test_the_same_value_maps_to_the_same_pseudonym() -> None:
    """Determinism is seeded from the HMAC, so it holds even with a fresh store."""
    first, _ = pseudonymize_chunks([chunk(PII_TEXT)], build_pseudonymizer({"Laura Fernández": "PERSON"}))
    second, _ = pseudonymize_chunks([chunk(PII_TEXT)], build_pseudonymizer({"Laura Fernández": "PERSON"}))

    assert first[0].text == second[0].text


def test_different_values_map_to_different_pseudonyms() -> None:
    pseudonymizer = build_pseudonymizer(
        {"Laura Fernández": "PERSON", "Carlos Ruiz": "PERSON"}
    )
    text = "Owner: Laura Fernández. Reviewer: Carlos Ruiz."

    result, _ = pseudonymize_chunks([chunk(text)], pseudonymizer)

    assert "Laura Fernández" not in result[0].text
    assert "Carlos Ruiz" not in result[0].text
    # Two distinct names must not collapse onto one pseudonym.
    assert result[0].text.count("Owner:") == 1


def test_a_different_salt_yields_a_different_pseudonym() -> None:
    """Salt rotation is the GDPR lever; it must actually change the output."""
    a, _ = pseudonymize_chunks(
        [chunk(PII_TEXT)], build_pseudonymizer({"Laura Fernández": "PERSON"}, salt="salt-a")
    )
    b, _ = pseudonymize_chunks(
        [chunk(PII_TEXT)], build_pseudonymizer({"Laura Fernández": "PERSON"}, salt="salt-b")
    )

    assert a[0].text != b[0].text


def test_the_mapping_store_never_receives_plaintext() -> None:
    """Only the HMAC may reach the store; that is what makes forget auditable."""
    store = InMemoryMappingStore()
    pseudonymizer = ConsistentPseudonymizer(
        analyzer=SubstringAnalyzer({"Laura Fernández": "PERSON"}),
        mapping_store=store,
        salt="test-salt",
        faker_locale="es_ES",
    )

    pseudonymize_chunks([chunk(PII_TEXT)], pseudonymizer)

    serialized_keys = repr(store.keys())
    assert "Laura Fernández" not in serialized_keys
    assert "Laura" not in serialized_keys
    assert all(len(original_hash) == 64 for _, original_hash in store.keys())


def test_a_failing_pseudonymizer_raises_instead_of_leaking_plaintext() -> None:
    class Exploding:
        def pseudonymize(self, text: str, **_: object) -> object:
            raise RuntimeError("analyzer down")

    with pytest.raises(PseudonymizationFailed, match="could not pseudonymize"):
        pseudonymize_chunks([chunk(PII_TEXT)], Exploding())


def test_a_pseudonymizer_returning_no_text_is_refused() -> None:
    class Empty:
        def pseudonymize(self, text: str, **_: object) -> object:
            return SimpleNamespace(applied=[])

    with pytest.raises(PseudonymizationFailed, match="returned no text"):
        pseudonymize_chunks([chunk(PII_TEXT)], Empty())


# --- the service: the flag actually reaches the write -----------------------


def one_budget(budget_id: str = "BUD-2024-001") -> Budget:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[0]
    return Budget.model_validate({**payload, "budget_id": budget_id})


class FakeSession:
    committed = False
    rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def begin(self):
        session = self

        class _Tx:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _Tx()


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSession:
        return self.session


class RecordingStore:
    """Keeps exactly what would have been written."""

    def __init__(self) -> None:
        self.written: list = []

    async def find_document_id(self, session, source_path: str) -> int | None:
        return None

    async def persist_document_with_chunks(
        self, session, *, source_path, document_type, doc_metadata, embedded_chunks
    ) -> int:
        self.written = list(embedded_chunks)
        return 1


class EchoEmbedder:
    def embed_many(self, chunks):
        from app.embedding_pipeline.schemas import EmbeddedChunk

        return [
            EmbeddedChunk(**chunk.model_dump(), embedding=[0.1] * 1536) for chunk in chunks
        ]


class BudgetWithPii:
    """The sample budget with a person name injected into one component."""

    @staticmethod
    def build() -> tuple[Budget, str]:
        payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[0]
        name = "Laura Fernández"
        payload["client_metadata"]["name"] = name
        payload["components"][0]["description"] += f" Escalation contact: {name}."
        return Budget.model_validate(payload), name


def build_service(*, pseudonymizer_factory):
    store = RecordingStore()
    service = RagIngestService(
        chunker=__import__(
            "app.embedding_pipeline.chunking.structural", fromlist=["JSONStructuralChunker"]
        ).JSONStructuralChunker(),
        embedder=EchoEmbedder(),
        session_factory=FakeSessionFactory(),
        store=store,
        pseudonymizer_factory=pseudonymizer_factory,
    )
    return service, store


def test_service_with_the_flag_off_stores_verbatim() -> None:
    """The default must not change what is stored — distances depend on it."""
    import asyncio

    budget, name = BudgetWithPii.build()
    service, store = build_service(
        pseudonymizer_factory=lambda: build_pseudonymizer({name: "PERSON"})
    )

    response = asyncio.run(
        service.ingest(
            source_path="t::1", document_type="historical_budget", budget=budget
        )
    )

    assert any(name in chunk.text for chunk in store.written)
    assert response.entities_pseudonymized == 0


def test_service_with_the_flag_on_stores_no_plaintext() -> None:
    import asyncio

    budget, name = BudgetWithPii.build()
    service, store = build_service(
        pseudonymizer_factory=lambda: build_pseudonymizer({name: "PERSON"})
    )

    response = asyncio.run(
        service.ingest(
            source_path="t::2",
            document_type="historical_budget",
            budget=budget,
            pseudonymize=True,
        )
    )

    written = " ".join(chunk.text for chunk in store.written)
    assert name not in written
    assert response.entities_pseudonymized >= 1
    # Everything else still arrives.
    assert len(store.written) == response.chunks_created


def test_service_refuses_when_pseudonymization_is_requested_but_unwired() -> None:
    """No pseudonymizer + flag on must fail loudly, never store plaintext."""
    import asyncio

    from app.embedding_pipeline.ingest_service import PseudonymizationUnavailable

    budget, _ = BudgetWithPii.build()
    service, store = build_service(pseudonymizer_factory=None)

    with pytest.raises(PseudonymizationUnavailable):
        asyncio.run(
            service.ingest(
                source_path="t::3",
                document_type="historical_budget",
                budget=budget,
                pseudonymize=True,
            )
        )

    assert store.written == []
