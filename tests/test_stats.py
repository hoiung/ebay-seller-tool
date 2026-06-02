"""Tests for ebay.stats.percentile — the shared quantile helper (#40 AC2.3).

Pins both methods so the dedup of browse.py (nearest_rank) and
content_benchmark.py (inclusive) can never silently drift apart.
"""

from __future__ import annotations

import pytest

from ebay.stats import percentile


@pytest.mark.parametrize(
    ("prices", "p25_idx", "p75_idx"),
    [
        # rank expectations mirror test_browse.py's pinned N=2..5 spec
        ([10.0, 20.0], 0, 1),  # N=2: p25==min, p75==max
        ([10.0, 20.0, 30.0], 0, 2),  # N=3
        ([10.0, 20.0, 30.0, 40.0], 1, 3),  # N=4
        ([10.0, 20.0, 30.0, 40.0, 50.0], 1, 3),  # N=5
    ],
)
def test_nearest_rank_matches_browse_ranks(prices, p25_idx, p75_idx) -> None:
    assert percentile(prices, 0.25, method="nearest_rank", presorted=True) == prices[p25_idx]
    assert percentile(prices, 0.75, method="nearest_rank", presorted=True) == prices[p75_idx]


def test_nearest_rank_sorts_when_not_presorted() -> None:
    unsorted = [40.0, 10.0, 30.0, 20.0]
    assert percentile(unsorted, 0.25, method="nearest_rank") == 20.0  # sorted[1]
    assert percentile(unsorted, 0.75, method="nearest_rank") == 40.0  # sorted[3]


def test_inclusive_interpolates() -> None:
    # The exact divergence the AC cites: nearest_rank p25 == 20.0 but inclusive
    # p25 == 17.5 on the identical 4-element pool.
    pool = [10.0, 20.0, 30.0, 40.0]
    assert percentile(pool, 0.25, method="inclusive") == 17.5
    assert percentile(pool, 0.50, method="inclusive") == 25.0
    assert percentile(pool, 0.75, method="inclusive") == 32.5
    assert percentile(pool, 0.25, method="nearest_rank", presorted=True) == 20.0


def test_empty_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        percentile([], 0.25, method="nearest_rank")


def test_unknown_method_raises() -> None:
    with pytest.raises(ValueError, match="unknown method"):
        percentile([1.0, 2.0], 0.25, method="bogus")


def test_nearest_rank_rejects_p50() -> None:
    with pytest.raises(ValueError, match="nearest_rank supports"):
        percentile([1.0, 2.0], 0.50, method="nearest_rank")
