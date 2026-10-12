"""assign_tiers: the relative tier rule behind semantic search."""

from __future__ import annotations

import pytest

from mail_verdict.embeddings.search import assign_tiers


@pytest.mark.parametrize(
    ("scores", "tiers"),
    [
        ([], []),
        # Three or fewer: no spread to judge by, everything is likely.
        ([0.5, 0.4, 0.3], [1, 1, 1]),
        # No spread at all.
        ([0.5, 0.5, 0.5, 0.5, 0.5], [1, 1, 1, 1, 1]),
        (
            [0.70, 0.68, 0.66, 0.60, 0.55, 0.50, 0.45, 0.40],
            [1, 1, 1, 2, None, None, None, None],
        ),
        (
            [0.62, 0.60, 0.58, 0.57, 0.56, 0.54, 0.52, 0.50, 0.48, 0.46],
            [1, 1, 1, 1, 2, None, None, None, None, None],
        ),
        # Input order is kept, not the ranking order.
        (
            [0.46, 0.70, 0.55, 0.68, 0.40, 0.66, 0.50, 0.60],
            [None, 1, None, 1, None, 1, None, 2],
        ),
    ],
)
def test_assign_tiers(scores: list[float], tiers: list[int | None]) -> None:
    assert assign_tiers(scores) == tiers


def test_a_single_outlier_does_not_squash_the_rest() -> None:
    """The anchor is the third-best score, so one runaway hit leaves the
    hits behind it in tier one."""
    scores = [0.95, 0.60, 0.59, 0.58, 0.50, 0.40, 0.30, 0.20]
    assert assign_tiers(scores)[:4] == [1, 1, 1, 1]
