"""Unit tests for reciprocal rank fusion (Session 10, T5.1).

Pure function: no fakes, no I/O. The arithmetic is pinned exactly because a
1-based/0-based slip or a missed dedupe is invisible in normal use — it only
reorders hybrid results, silently, and the four-configuration table then reports
a ranking that was computed with the wrong formula.
"""

from __future__ import annotations

import pytest

from app.generation.rag.retrieval.fusion import (
    DEFAULT_RRF_K,
    reciprocal_rank_fusion,
)


def scores_by_id(result: list[tuple[int, float]]) -> dict[int, float]:
    return {chunk_id: score for chunk_id, score in result}


def test_default_k_is_the_cormack_constant() -> None:
    assert DEFAULT_RRF_K == 60


def test_single_ranking_scores_one_over_k_plus_one_based_position() -> None:
    result = reciprocal_rank_fusion([[10, 20, 30]], k=60)

    assert [chunk_id for chunk_id, _ in result] == [10, 20, 30]
    scores = scores_by_id(result)
    assert scores[10] == pytest.approx(1 / 61)
    assert scores[20] == pytest.approx(1 / 62)
    assert scores[30] == pytest.approx(1 / 63)


def test_rank_is_one_based_not_zero_based() -> None:
    """The first document scores 1/(k+1); 1/k would be off by one."""
    [(chunk_id, score)] = reciprocal_rank_fusion([[7]], k=60)

    assert chunk_id == 7
    assert score == pytest.approx(1 / 61)
    assert score != pytest.approx(1 / 60)


def test_two_rankings_sum_the_reciprocal_terms() -> None:
    result = reciprocal_rank_fusion([[1, 2], [2, 3]], k=60)

    scores = scores_by_id(result)
    # id 1: position 1 of list 1.
    assert scores[1] == pytest.approx(1 / 61)
    # id 2: position 2 of list 1 + position 1 of list 2.
    assert scores[2] == pytest.approx(1 / 62 + 1 / 61)
    # id 3: position 2 of list 2.
    assert scores[3] == pytest.approx(1 / 62)
    # A chunk ranked by both lists beats a chunk ranked first by only one.
    assert [chunk_id for chunk_id, _ in result] == [2, 1, 3]


def test_custom_k_changes_the_terms() -> None:
    scores = scores_by_id(reciprocal_rank_fusion([[1, 2]], k=10))
    assert scores[1] == pytest.approx(1 / 11)
    assert scores[2] == pytest.approx(1 / 12)


def test_duplicates_inside_one_ranking_score_once() -> None:
    """A repeated id contributes one term and consumes one position.

    The duplicate neither scores again nor pushes later documents down: a
    branch that emits ``[5, 5, 6]`` ranks exactly as ``[5, 6]``, so id 6 is at
    position 2 and scores ``1/(k+2)``. Pinned exactly on purpose — an
    implementation that let the duplicate consume a position would silently
    weaken every branch that ever repeats an id.
    """
    result = reciprocal_rank_fusion([[5, 5, 6]], k=60)

    scores = scores_by_id(result)
    assert scores[5] == pytest.approx(1 / 61)
    assert scores[6] == pytest.approx(1 / 62)
    assert [chunk_id for chunk_id, _ in result] == [5, 6]
    assert len(result) == 2


def test_a_duplicate_does_not_shift_later_positions() -> None:
    """``[5, 5, 7, 6]`` fuses to exactly the same scores as ``[5, 7, 6]``."""
    with_duplicate = scores_by_id(reciprocal_rank_fusion([[5, 5, 7, 6]], k=60))
    canonical = scores_by_id(reciprocal_rank_fusion([[5, 7, 6]], k=60))

    assert with_duplicate == pytest.approx(canonical)


def test_empty_inputs_are_empty() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []
    assert reciprocal_rank_fusion([[], [], []]) == []


def test_empty_branch_does_not_contribute() -> None:
    result = reciprocal_rank_fusion([[], [1]], k=60)

    assert len(result) == 1
    chunk_id, score = result[0]
    assert chunk_id == 1
    assert score == pytest.approx(1 / 61)


def test_a_document_in_both_lists_outranks_one_in_a_single_list() -> None:
    result = reciprocal_rank_fusion([[1, 2], [1, 3]], k=60)

    assert result[0][0] == 1
    scores = scores_by_id(result)
    assert scores[1] > scores[2]
    assert scores[1] > scores[3]


def test_ties_break_on_ascending_id() -> None:
    """Equal scores must not depend on dict order or list order."""
    result = reciprocal_rank_fusion([[2], [1]], k=60)

    scores = scores_by_id(result)
    assert scores[1] == scores[2]
    assert [chunk_id for chunk_id, _ in result] == [1, 2]


def test_output_is_stable_across_repeated_calls() -> None:
    rankings = [[3, 1, 2], [2, 3, 1], []]
    first = reciprocal_rank_fusion(rankings, k=60)

    for _ in range(5):
        assert reciprocal_rank_fusion(rankings, k=60) == first


def test_input_rankings_are_not_mutated() -> None:
    rankings = [[3, 1, 2], [2, 3, 1]]
    before = [list(ranking) for ranking in rankings]

    reciprocal_rank_fusion(rankings, k=60)

    assert rankings == before


def test_output_entries_are_int_float_tuples() -> None:
    result = reciprocal_rank_fusion([[1, 2]], k=60)
    assert all(isinstance(chunk_id, int) for chunk_id, _ in result)
    assert all(isinstance(score, float) for _, score in result)


@pytest.mark.parametrize("k", [0, -1, -60])
def test_non_positive_k_raises_value_error(k: int) -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([[1, 2]], k=k)
