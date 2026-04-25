"""Issue #14 Phase 1.2.1 — one-shot diagnostic.

Reads a previously-saved sweep cache (JSON list of comp listings or a v2 sweep
output) and reports the distribution of quality signals so the Layer-1 binary
threshold (`require_at_least_one_image`) can be calibrated against real data.

Output (stdout):
  feedback_pct distribution: count, min, p25, median, p75, max
  returns_accepted=False rate
  image_url is None rate
  image_url is None AND additional_image_count == 0 rate

This is NOT a regression test. Run on demand to verify Layer-1 thresholds are
not dropping >5% of legitimate comps. If the image-AND-no-additional rate is
>5%, the binary threshold should be deferred to Layer-2 deduction.

Usage:
    .venv/bin/python scripts/measure_comp_quality_distribution.py /tmp/raw_comps.json
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    idx = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * pct)))
    return sorted_vals[idx]


def main(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: {p} not found", file=sys.stderr)
        return 1
    data = json.loads(p.read_text())
    if isinstance(data, dict) and "listings" in data:
        comps = data["listings"]
    elif isinstance(data, list):
        comps = data
    else:
        print(f"ERROR: {p} top-level must be a list of comps or a fetch_competitor_prices dict")
        return 1

    n = len(comps)
    if n == 0:
        print("Empty comp pool — nothing to measure.")
        return 0

    feedback_pcts: list[float] = []
    returns_false = 0
    image_url_none = 0
    image_zero = 0
    for c in comps:
        try:
            pct = float(c.get("seller_feedback_pct"))
            feedback_pcts.append(pct)
        except (TypeError, ValueError):
            pass
        if c.get("returns_accepted") is False:
            returns_false += 1
        if c.get("image_url") is None:
            image_url_none += 1
            if (c.get("additional_image_count") or 0) == 0:
                image_zero += 1

    feedback_pcts.sort()
    print(f"Comp pool: N={n}")
    if feedback_pcts:
        print(
            f"  seller_feedback_pct (N={len(feedback_pcts)}): "
            f"min={feedback_pcts[0]:.2f}, p25={_percentile(feedback_pcts, 0.25):.2f}, "
            f"median={statistics.median(feedback_pcts):.2f}, "
            f"p75={_percentile(feedback_pcts, 0.75):.2f}, max={feedback_pcts[-1]:.2f}"
        )
    else:
        print("  seller_feedback_pct: NO data")
    print(f"  returns_accepted=False: {returns_false}/{n} ({100.0 * returns_false / n:.1f}%)")
    print(f"  image_url is None:      {image_url_none}/{n} ({100.0 * image_url_none / n:.1f}%)")
    print(
        f"  image_url None AND additional_image_count=0: "
        f"{image_zero}/{n} ({100.0 * image_zero / n:.1f}%)"
    )
    if image_zero / n > 0.05:
        print(
            "WARNING: image-zero rate > 5% — Layer-1 binary threshold may drop too "
            "many legitimate comps; consider deferring to Layer-2 deduction."
        )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
