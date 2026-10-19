"""Documents, loader and parsers.

Hermetic: files come from ``tmp_path`` or from the seed corpus on disk, never
from the network. The seed corpus is read-only input, and these tests assert on
its documented shape (6 budgets, 3 transcripts).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.ingestion.catalog.models import CatalogDecision, CatalogSource
from app.ingestion.documents.models import RAW_RECORD_KEY, Document
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.parsers.budget_json import BudgetJsonParser
from app.ingestion.parsers.protocol import ParseContext
from app.ingestion.parsers.registry import ParserRegistry, default_registry
from app.ingestion.parsers.transcript_txt import (
    TAGGED_MODE_MIN_TURNS,
    TranscriptTxtParser,
)

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"


def make_source(**overrides) -> CatalogSource:
    payload = {
        "name": "presupuestos_json",
        "location": "budgets",
        "format": "json",
        "quality": {
            "completeness": 4,
            "consistency": 3,
            "actuality": 5,
            "reliability": 4,
        },
        "sensitivity": {
            "has_pii": True,
            "pii_flags": ["PERSON", "EMAIL"],
            "access_level": "confidential",
        },
        "lineage": ["Generado por el CRM."],
        "decision": CatalogDecision.INCLUDE.value,
    }
    payload.update(overrides)
    return CatalogSource.model_validate(payload)


def make_context(source: CatalogSource | None = None) -> ParseContext:
    return ParseContext(
        source=source or make_source(),
        source_version="1.0.0",
        ingested_at=datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc),
    )


class Blob:
    """Minimal stand-in for ``LoadedBlob`` built from text."""

    def __init__(self, text: str, relative_path: str = "budgets/x.json") -> None:
        self.relative_path = relative_path
        self.bytes_ = text.encode("utf-8")


# --------------------------------------------------------------------------- #
# BudgetJsonParser
# --------------------------------------------------------------------------- #
def test_json_parser_renders_markdown_with_semantic_sections():
    blob = Blob((SEED / "budgets" / "BUDGET-2024-0001.json").read_text(encoding="utf-8"))
    parser = BudgetJsonParser()

    documents = list(parser.parse(blob, make_context()))

    assert len(documents) == 1
    text = documents[0].text
    assert text.startswith("# Presupuesto BUDGET-2024-0001")
    for heading in ("## Cliente", "## Total", "## Fases", "## Notas"):
        assert heading in text
    # Numbers survive the rendering; only the framing changes.
    assert "45000" in text
    assert "Acme España S.L." in text
    assert "Firmado: 2024-02-14" in text
    assert "**Discovery**" in text


def test_json_parser_keeps_the_raw_record_for_cleaning():
    payload = json.loads(
        (SEED / "budgets" / "BUDGET-2024-0002.json").read_text(encoding="utf-8")
    )
    blob = Blob(json.dumps(payload))
    parser = BudgetJsonParser()

    document = next(iter(parser.parse(blob, make_context())))

    raw = document.metadata.extra[RAW_RECORD_KEY]
    assert raw == payload
    assert document.metadata.raw_record == payload
    # The raw record, not the markdown, is what cleaning/validation sees.
    assert raw["signed_at"] == "12/03/2024"
    assert raw["total_amount"] == 28000


def test_json_parser_metadata_carries_source_lineage_and_sensitivity():
    document = next(
        iter(
            BudgetJsonParser().parse(
                Blob((SEED / "budgets" / "BUDGET-2024-0003.json").read_text(encoding="utf-8")),
                make_context(),
            )
        )
    )
    metadata = document.metadata

    assert metadata.source_name == "presupuestos_json"
    assert metadata.source_version == "1.0.0"
    assert metadata.sensitivity_access_level == "confidential"
    assert metadata.sensitivity_pii_flags == ["PERSON", "EMAIL"]
    assert metadata.lineage == ["Generado por el CRM."]
    assert metadata.ingested_at.year == 2026
    assert metadata.location == "budgets/x.json"
    assert metadata.extra["budget_id"] == "BUDGET-2024-0003"
    assert metadata.extra["client_code"] == "CLI-0103"


def test_json_document_id_is_stable_and_source_scoped():
    blob = Blob((SEED / "budgets" / "BUDGET-2024-0001.json").read_text(encoding="utf-8"))
    parser = BudgetJsonParser()

    first = next(iter(parser.parse(blob, make_context())))
    second = next(iter(parser.parse(blob, make_context())))

    assert first.id == second.id
    assert first.id == "presupuestos_json:BUDGET-2024-0001:budgets/x.json"


def test_json_parser_accepts_a_list_of_records():
    blob = Blob(
        json.dumps(
            [
                {"budget_id": "BUDGET-2024-0001", "total_amount": 1},
                {"budget_id": "BUDGET-2024-0002", "total_amount": 2},
            ]
        )
    )

    documents = list(BudgetJsonParser().parse(blob, make_context()))

    assert [doc.metadata.extra["budget_id"] for doc in documents] == [
        "BUDGET-2024-0001",
        "BUDGET-2024-0002",
    ]


def test_json_parser_rejects_a_non_object_record():
    with pytest.raises(ValueError, match="JSON object"):
        list(BudgetJsonParser().parse(Blob("[1, 2]"), make_context()))


def test_json_parser_round_trips_the_whole_seed_folder():
    loader = FileSystemLoader(SEED)
    parser = BudgetJsonParser()

    documents = [
        document
        for blob in loader.iter_blobs("budgets", {"json"})
        for document in parser.parse(blob, make_context())
    ]

    assert len(documents) == 6
    assert all(document.text.startswith("# Presupuesto BUDGET-2024-") for document in documents)


# --------------------------------------------------------------------------- #
# TranscriptTxtParser
# --------------------------------------------------------------------------- #
TAGGED_TXT = "\n".join(
    [
        "[10:00:12] Laura Fernández: Buenos días, gracias por la reunión.",
        "[10:00:48] Diego Marín: Hola Laura, tengo el presupuesto abierto.",
        "[10:01:30] Laura Fernández: Perfecto, cerramos aquí.",
    ]
)

TWO_TAG_TXT = "\n".join(
    [
        "[10:00:12] Laura Fernández: Buenos días.",
        "[10:00:48] Diego Marín: Hola Laura.",
        "Texto suelto sin marca horaria.",
    ]
)

LEGACY_TXT = (
    "Reunión de seguimiento sin marcas horarias.\n\n"
    "Primer bloque de la reunión con su propio contenido.\n\n"
    "Segundo bloque, separado por una línea en blanco.\n"
)


def test_tagged_mode_produces_one_document_per_turn():
    documents = list(TranscriptTxtParser().parse(Blob(TAGGED_TXT, "transcripts/a.txt"), make_context()))

    assert len(documents) == 3
    assert [doc.metadata.extra["speaker"] for doc in documents] == [
        "Laura Fernández",
        "Diego Marín",
        "Laura Fernández",
    ]
    assert [doc.metadata.extra["timestamp"] for doc in documents] == [
        "10:00:12",
        "10:00:48",
        "10:01:30",
    ]
    assert all(doc.metadata.extra["format_mode"] == "tagged" for doc in documents)
    assert documents[0].text == "Buenos días, gracias por la reunión."
    assert documents[0].id.endswith(":turn-0000")


def test_three_matching_lines_is_the_tagged_threshold():
    assert TAGGED_MODE_MIN_TURNS == 3
    tagged = list(TranscriptTxtParser().parse(Blob(TAGGED_TXT), make_context()))
    assert len(tagged) == 3
    assert tagged[0].metadata.extra["format_mode"] == "tagged"

    # Two tagged lines are not enough: the file falls back to legacy blocks.
    legacy = list(TranscriptTxtParser().parse(Blob(TWO_TAG_TXT), make_context()))
    assert legacy[0].metadata.extra["format_mode"] == "legacy"


def test_legacy_mode_splits_on_blank_lines_and_fabricates_no_speaker():
    documents = list(
        TranscriptTxtParser().parse(Blob(LEGACY_TXT, "transcripts/legacy.txt"), make_context())
    )

    assert len(documents) == 3
    for document in documents:
        assert "speaker" not in document.metadata.extra
        assert document.metadata.extra["format_mode"] == "legacy"
    assert documents[1].text.startswith("Primer bloque")
    assert documents[1].metadata.extra["block_index"] == 1
    assert documents[0].id.endswith(":block-0000")


def test_transcript_parser_reads_the_seed_corpus_in_both_modes():
    loader = FileSystemLoader(SEED)
    parser = TranscriptTxtParser()

    modes = set()
    total = 0
    for blob in loader.iter_blobs("transcripts", {"txt"}):
        for document in parser.parse(blob, make_context()):
            modes.add(document.metadata.extra["format_mode"])
            total += 1

    assert modes == {"tagged", "legacy"}
    assert total > 15  # 7 + 6 turns + 5 legacy blocks


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_default_registry_registers_only_json_and_txt():
    registry = default_registry()

    assert registry.formats() == {"json", "txt"}
    assert isinstance(registry.get("json"), BudgetJsonParser)
    assert isinstance(registry.get("txt"), TranscriptTxtParser)


def test_registry_rejects_duplicate_format():
    registry = ParserRegistry()
    registry.register(BudgetJsonParser())

    with pytest.raises(ValueError, match="already has a registered parser"):
        registry.register(BudgetJsonParser())


def test_registry_raises_for_unknown_format():
    with pytest.raises(KeyError, match="No parser registered"):
        default_registry().get("xlsx")


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def test_loader_yields_only_matching_extensions(tmp_path):
    root = tmp_path / "seed"
    (root / "budgets").mkdir(parents=True)
    (root / "budgets" / "a.json").write_text("{}", encoding="utf-8")
    (root / "budgets" / "b.txt").write_text("hola", encoding="utf-8")
    (root / "budgets" / "c.xlsx").write_bytes(b"not really")

    blobs = list(FileSystemLoader(root).iter_blobs("budgets", {"json"}))

    assert [blob.relative_path for blob in blobs] == ["budgets/a.json"]


def test_loader_raises_when_location_is_missing(tmp_path):
    root = tmp_path / "seed"
    root.mkdir()

    with pytest.raises(FileNotFoundError):
        list(FileSystemLoader(root).iter_blobs("no_existe", {"json"}))


def test_loader_rejects_parent_directory_traversal(tmp_path):
    root = tmp_path / "seed"
    root.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text('{"budget_id": "BUDGET-2024-9999"}', encoding="utf-8")

    loader = FileSystemLoader(root)

    with pytest.raises(ValueError, match="escapes the configured data root"):
        list(loader.iter_blobs("../secret.json", {"json"}))
    with pytest.raises(ValueError, match="escapes the configured data root"):
        loader.resolve("../")


def test_loader_rejects_absolute_paths_outside_the_root(tmp_path):
    root = tmp_path / "seed"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(ValueError, match="escapes the configured data root"):
        list(FileSystemLoader(root).iter_blobs(str(outside), {"json"}))


def test_loader_accepts_a_single_file_location(tmp_path):
    root = tmp_path / "seed"
    root.mkdir()
    (root / "uno.json").write_text("{}", encoding="utf-8")

    blobs = list(FileSystemLoader(root).iter_blobs("uno.json", {"json"}))

    assert [blob.relative_path for blob in blobs] == ["uno.json"]
    assert blobs[0].bytes_ == b"{}"


def test_loader_relative_paths_are_posix_style(tmp_path):
    root = tmp_path / "seed"
    (root / "nested" / "deep").mkdir(parents=True)
    (root / "nested" / "deep" / "x.json").write_text("{}", encoding="utf-8")

    blobs = list(FileSystemLoader(root).iter_blobs("nested", {"json"}))

    assert [blob.relative_path for blob in blobs] == ["nested/deep/x.json"]


# --------------------------------------------------------------------------- #
# Document model
# --------------------------------------------------------------------------- #
def test_document_model_forbids_extra_fields():
    with pytest.raises(Exception):
        Document(id="x", text="y", metadata={}, unexpected=True)
