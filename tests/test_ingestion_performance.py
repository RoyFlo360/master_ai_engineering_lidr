"""Ingestion performance: seed throughput, linear scaling, factory caching.

Guards are generous (an order of magnitude above what the code does) because a
performance test that fails on a loaded laptop teaches nothing. What they do
catch is an accidental O(n²): re-reading the catalog per document, re-parsing a
blob per record, or sorting inside a loop.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.ingestion.catalog.models import CatalogSource
from app.ingestion.dependencies import get_catalog
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.parsers.protocol import ParseContext
from app.ingestion.parsers.registry import default_registry
from app.ingestion.parsers.transcript_txt import TranscriptTxtParser

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"


def _source(fmt: str, name: str, location: str) -> CatalogSource:
    return CatalogSource.model_validate(
        {
            "name": name,
            "location": location,
            "format": fmt,
            "quality": {
                "completeness": 4,
                "consistency": 4,
                "actuality": 4,
                "reliability": 4,
            },
            "sensitivity": {"has_pii": False},
            "decision": "include",
        }
    )


def _context(source: CatalogSource) -> ParseContext:
    return ParseContext(
        source=source,
        source_version="1.0.0",
        ingested_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
    )


def _parse_all(root: Path, location: str, fmt: str) -> int:
    loader = FileSystemLoader(root)
    parser = default_registry().get(fmt)
    source = _source(fmt, "presupuestos_json" if fmt == "json" else "transcripciones_txt", location)
    context = _context(source)
    count = 0
    for blob in loader.iter_blobs(location, {fmt}):
        count += sum(1 for _ in parser.parse(blob, context))
    return count


def test_seed_corpus_parses_fast_enough_to_serve_requests():
    started = time.perf_counter()
    budgets = _parse_all(SEED, "budgets", "json")
    transcripts = _parse_all(SEED, "transcripts", "txt")
    elapsed = time.perf_counter() - started

    total = budgets + transcripts
    assert budgets == 6
    assert total == 24  # 6 budgets + 7 + 6 turns + 5 legacy blocks
    assert elapsed < 5.0, f"seed corpus took {elapsed:.2f}s"
    assert total / elapsed > 50, f"{total / elapsed:.0f} docs/s is too slow"


def _write_corpus(root: Path, count: int) -> None:
    (root / "budgets").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        payload = {
            "budget_id": f"BUDGET-2024-{index:04d}",
            "client_name": f"Cliente {index}",
            "client_code": f"CLI-{index:04d}",
            "currency": "EUR",
            "total_amount": 1000 + index,
            "signed_at": "2024-01-01",
            "phases": [{"name": "Fase", "weeks": 1, "amount": 1000}],
            "contact": "Contacto",
            "contact_email": "contacto@empresa.local",
            "notes": "nota",
        }
        (root / "budgets" / f"BUDGET-2024-{index:04d}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


def test_parsing_scales_linearly_with_the_number_of_records(tmp_path):
    small = tmp_path / "small"
    large = tmp_path / "large"
    _write_corpus(small, 60)
    _write_corpus(large, 120)

    start_small = time.perf_counter()
    assert _parse_all(small, "budgets", "json") == 60
    small_elapsed = time.perf_counter() - start_small

    start_large = time.perf_counter()
    assert _parse_all(large, "budgets", "json") == 120
    large_elapsed = time.perf_counter() - start_large

    # Doubling the corpus must not do much worse than doubling the time.
    assert large_elapsed < 4 * small_elapsed + 0.5, (
        f"non-linear: {small_elapsed:.4f}s for 60 vs {large_elapsed:.4f}s for 120"
    )


def test_parser_reuse_is_cheaper_than_re_parsing_from_scratch():
    """Sanity guard: the parser object holds no per-run state that grows."""
    parser = TranscriptTxtParser()
    loader = FileSystemLoader(SEED)
    context = _context(_source("txt", "transcripciones_txt", "transcripts"))
    blobs = list(loader.iter_blobs("transcripts", {"txt"}))

    first = sum(1 for blob in blobs for _ in parser.parse(blob, context))
    second = sum(1 for blob in blobs for _ in parser.parse(blob, context))

    assert first == second == 18  # 7 + 6 turns + 5 legacy blocks


def test_catalog_factory_is_cached_and_returns_the_same_instance():
    before = get_catalog.cache_info()

    first = get_catalog()
    second = get_catalog()

    after = get_catalog.cache_info()
    assert first is second
    assert after.hits >= before.hits + 1
    assert after.misses == before.misses or after.misses == before.misses + 1


def test_loader_does_not_read_the_whole_tree_twice(tmp_path):
    _write_corpus(tmp_path, 40)
    loader = FileSystemLoader(tmp_path)

    paths = [blob.relative_path for blob in loader.iter_blobs("budgets", {"json"})]

    assert len(paths) == 40
    assert len(set(paths)) == 40
