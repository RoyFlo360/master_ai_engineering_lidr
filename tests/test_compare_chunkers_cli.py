"""Tests for ``scripts/compare_chunkers.py`` (spec 013).

The CLI is imported as a module and driven with a fake embedder, so nothing
leaves the process. These pin the parsing helpers, the flow through
``main()`` and the Markdown report shape.
"""

from __future__ import annotations

import json

import pytest

from scripts import compare_chunkers as cli


def test_normalize_maps_hyphens_and_the_fixed_alias():
    assert cli.normalize("fixed-size") == "fixed_size"
    assert cli.normalize("fixed") == "fixed_size"
    assert cli.normalize("sentence-window") == "sentence_window"
    assert cli.normalize("structural") == "structural"


def test_parse_strategies_all_returns_the_registry_order():
    from app.dependencies import ALL_STRATEGIES

    assert cli.parse_strategies("all") == list(ALL_STRATEGIES)
    assert cli.parse_strategies("structural,fixed-size") == ["structural", "fixed_size"]


def test_load_queries_all_and_inline():
    from_file = cli.load_queries("all")
    assert len(from_file) == 6
    assert cli.load_queries("a, b ,c") == ["a", "b", "c"]


def test_load_budgets_respects_the_limit():
    assert len(cli.load_budgets(None)) == 17
    assert len(cli.load_budgets(2)) == 2


class FakeEmbedder:
    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def embed_many(self, chunks):
        from app.embedding_pipeline.schemas import EmbeddedChunk

        return [
            EmbeddedChunk(**chunk.model_dump(), embedding=[0.5] * self.dim) for chunk in chunks
        ]

    def embed_one(self, text: str) -> list[float]:
        return [0.5] * self.dim


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(cli, "build_embedder", lambda dimensions=None: FakeEmbedder())


def test_main_prints_stats_without_touching_a_provider(no_network, capsys):
    code = cli.main(
        ["--strategies", "structural,fixed-size", "--show-stats", "--limit", "2"]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "strategy" in out
    assert "structural" in out
    assert "fixed_size" in out


def test_main_writes_an_english_markdown_report(no_network, tmp_path):
    target = tmp_path / "COMPARISON_REPORT.md"
    code = cli.main(
        [
            "--strategies",
            "structural",
            "--queries",
            "OAuth authentication",
            "--show-top-k",
            "2",
            "--limit",
            "1",
            "--output",
            str(target),
        ]
    )

    assert code == 0
    text = target.read_text(encoding="utf-8")
    assert "# Chunking comparison report" in text
    assert "## Statistics per strategy" in text
    assert "| strategy | n_chunks |" in text
    assert "### OAuth authentication" in text
    # English, not the reference's Spanish.
    assert "Estadísticas" not in text
    assert "Consultas" not in text


def test_unknown_strategy_exits_nonzero(no_network, capsys):
    assert cli.main(["--strategies", "nope", "--limit", "1"]) == 2
    assert "unknown strategy" in capsys.readouterr().err.lower()


def test_render_report_escapes_pipes_in_ids():
    stats = {
        "structural": cli.ChunkingComparator.stats_for("structural", [], 0.0, 0.0)
    }
    text = cli.render_report(stats, {}, budgets=0, strategies=["structural"], top_k=3)
    assert "| structural | 0 |" in text
    assert "nothing is persisted" in text.lower()
