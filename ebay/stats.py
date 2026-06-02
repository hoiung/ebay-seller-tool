"""Shared quantile helper (#40 AC2.3).

Single definition of the quantile arithmetic previously reimplemented across
ebay/browse.py (price-distribution display p25/p75 + IQR outlier-fence q1/q3 —
three sites) and ebay/content_benchmark.py (photo/duration benchmarks). Two
methods are supported because the call sites have distinct, test-pinned
semantics:

  * "nearest_rank" — browse.py's historical clamped index form
    (p25 = sorted[n // 4], p75 = sorted[(3 * n) // 4]). test_browse.py pins
    these exact ranks for N = 2..5, so the display + fence behaviour is
    preserved byte-for-byte. The max()/min() clamps are defensive no-ops
    (n // 4 >= 0 always; (3 * n) // 4 <= n - 1 for all n >= 1) retained for
    clarity.
  * "inclusive" — statistics.quantiles(..., method="inclusive"), the
    interpolated method content benchmarks have always used.

Centralising means a future change to either method happens in one place
rather than across three browse sites plus content_benchmark.
"""

from __future__ import annotations

import statistics

_INCLUSIVE_INDEX = {0.25: 0, 0.50: 1, 0.75: 2}


def percentile(
    values: list[float],
    q: float,
    *,
    method: str = "nearest_rank",
    presorted: bool = False,
) -> float:
    """Return the q-quantile of a non-empty list of values.

    Args:
        values: numeric samples. Must be non-empty; the inclusive method
            additionally requires len >= 2 (statistics.quantiles contract).
        q: quantile — {0.25, 0.75} for nearest_rank, {0.25, 0.50, 0.75} for
            inclusive.
        method: "nearest_rank" (clamped index) or "inclusive" (interpolated).
        presorted: skip the internal sort when ``values`` is already ascending
            (browse passes lists it already sorted for min/max/median). Has no
            effect for the inclusive method, which sorts inside
            statistics.quantiles regardless.
    """
    if not values:
        raise ValueError("percentile() requires a non-empty list")
    if method == "nearest_rank":
        sv = values if presorted else sorted(values)
        n = len(sv)
        if q == 0.25:
            return sv[max(0, n // 4)]
        if q == 0.75:
            return sv[min(n - 1, (3 * n) // 4)]
        raise ValueError(f"nearest_rank supports q in {{0.25, 0.75}}, got {q}")
    if method == "inclusive":
        index = _INCLUSIVE_INDEX.get(q)
        if index is None:
            raise ValueError(f"inclusive supports q in {{0.25, 0.50, 0.75}}, got {q}")
        return statistics.quantiles(values, n=4, method="inclusive")[index]
    raise ValueError(f"unknown method {method!r}")
