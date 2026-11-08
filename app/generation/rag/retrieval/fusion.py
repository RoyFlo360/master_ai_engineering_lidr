"""Reciprocal Rank Fusion (Cormack et al., 2009).

Pure and synchronous: no database, no logger, no configuration. That is what
makes it trivially testable and what lets the evaluation harness trust it across
the four retrieval configurations.

The score for a document is ``sum(1 / (k + rank))`` over the rankings that
contain it. Ranks are **1-based** here. The reference implementation this
session adapts used 0-based positions, which silently makes the first result of
every branch worth ``1/k`` instead of ``1/(k+1)`` — a constant shift on a single
document, but a different relative weight once a document appears in both
branches. We do not reproduce that.
"""

from __future__ import annotations

#: Cormack et al.'s default. Larger values flatten the contribution of the top
#: ranks, so a chunk must rank well in BOTH branches to beat a one-branch winner.
DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    rankings: list[list[int]], *, k: int = DEFAULT_RRF_K
) -> list[tuple[int, float]]:
    """Fuse ranked id lists into one list of ``(chunk_id, score)``.

    Args:
        rankings: One ranked list of ``chunk_id`` per branch, best first.
        k: Smoothing constant; must be positive.

    Returns:
        ``(chunk_id, score)`` sorted by descending score, ties broken by
        ascending ``chunk_id`` so the output is deterministic.

    Raises:
        ValueError: when ``k <= 0`` (the formula divides by ``k + rank``, and a
            non-positive ``k`` can yield a zero denominator at rank 1).
    """
    if k <= 0:
        raise ValueError("reciprocal_rank_fusion requires k > 0")

    scores: dict[int, float] = {}
    for ranking in rankings:
        seen: set[int] = set()
        position = 0
        for chunk_id in ranking:
            # Duplicates inside ONE ranking are ignored: they neither score again
            # nor consume a rank. Otherwise a single repeated id would push every
            # later document one position down and weaken the branch.
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            position += 1
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position)

    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
