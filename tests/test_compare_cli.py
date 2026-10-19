"""Tests for ``scripts/compare.py``: the cosine math and the CLI contract.

Hermetic: no provider call. The CLI's happy path replaces ``OpenAIEmbedder``
with a stub, and the missing-key path replaces the settings.
"""

from __future__ import annotations

import math

import pytest

from scripts import compare


def test_identical_vectors_score_one() -> None:
    assert compare.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_orthogonal_vectors_score_zero() -> None:
    assert compare.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_opposite_vectors_score_minus_one() -> None:
    assert compare.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_zero_vector_scores_zero_instead_of_raising() -> None:
    assert compare.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_scale_invariance() -> None:
    """Cosine measures direction, so a scaled copy scores 1.0."""
    assert compare.cosine_similarity([1.0, 2.0], [10.0, 20.0]) == pytest.approx(1.0)


def test_length_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        compare.cosine_similarity([1.0, 2.0], [1.0])


def test_result_always_lies_in_the_cosine_range() -> None:
    value = compare.cosine_similarity([0.2, -0.5, 3.0], [1.0, 0.4, -2.0])
    assert -1.0 <= value <= 1.0
    assert not math.isnan(value)


class _StubEmbedder:
    """Returns a fixed vector per text so the CLI's output is predictable."""

    vectors = {
        "alpha": [1.0, 0.0],
        "beta": [1.0, 0.0],
    }

    def __init__(self, *, client=None, model=None) -> None:
        pass

    def embed_one(self, text: str) -> list[float]:
        return self.vectors[text]


def test_cli_prints_the_similarity(monkeypatch, capsys) -> None:
    monkeypatch.setattr(compare, "OpenAIEmbedder", _StubEmbedder)
    monkeypatch.setattr(
        compare, "get_settings", lambda: _Settings(OPENAI_API_KEY="sk-test")
    )

    exit_code = compare.main(["--text-a", "alpha", "--text-b", "beta"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Text A: alpha" in out
    assert "Text B: beta" in out
    assert "Cosine similarity: 1.0000" in out


class _Settings:
    def __init__(self, *, OPENAI_API_KEY: str | None) -> None:  # noqa: N803 - mirrors Settings
        self.OPENAI_API_KEY = OPENAI_API_KEY
        self.EMBEDDING_MODEL = "text-embedding-3-small"


def test_cli_without_a_key_exits_non_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(compare, "get_settings", lambda: _Settings(OPENAI_API_KEY=None))

    exit_code = compare.main(["--text-a", "a", "--text-b", "b"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err


def test_cli_requires_both_texts(monkeypatch) -> None:
    """argparse exits 2 when a required flag is missing."""
    with pytest.raises(SystemExit) as exc:
        compare.main(["--text-a", "only-one"])
    assert exc.value.code == 2
