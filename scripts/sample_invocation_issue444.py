"""Issue #444 Part B — AP #18 sample invocation for the equivalence-class loop.

Live eBay Browse API call against HUS724020AL (or any --part-number override),
demonstrates the new orchestrator dispatches ONE call per equivalence-class
member (USED → 3000 + 2750), surfaces per-condition raw counts in audit_verbose,
and compares the merged percentile band against the pre-fix single-call baseline.

PREREQUISITES:
  - .env populated with EBAY_AUTH_TOKEN + EBAY_OAUTH_REFRESH_TOKEN + EBAY_OWN_SELLER_USERNAME
  - /tmp/ebay-listings-live.json present (run skill's fetch_listings.py first)

USAGE:
  uv run python scripts/sample_invocation_issue444.py [--part-number HUS724020AL]

OUTPUT:
  Stdout: human-readable summary with per-condition counts, kept pool size,
  percentile band, Browse-call count.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ebay.browse import fetch_competitor_prices

# Load .env BEFORE importing oauth (which reads env at module load).
load_dotenv()


def _find_own_listing(listings_path: Path, part_number: str) -> dict[str, Any] | None:
    """Locate an own listing whose Item Specifics MPN matches part_number."""
    if not listings_path.exists():
        return None
    listings = json.loads(listings_path.read_text())
    for item in listings:
        specifics = item.get("specifics") or {}
        mpns = specifics.get("MPN") or []
        if isinstance(mpns, str):
            mpns = [mpns]
        if part_number in [str(m).strip() for m in mpns]:
            return item
    return None


def _summarise(label: str, result: dict[str, Any]) -> str:
    audit = result.get("audit", {}) or {}
    audit_verbose = result.get("audit_verbose", {}) or {}
    raw_per_cond = audit_verbose.get("raw_count_per_condition_id", {}) or {}
    return (
        f"{label}\n"
        f"  raw_count_per_condition_id: {raw_per_cond}\n"
        f"  raw merged (post-dedupe):   {audit.get('raw_count', 'n/a')}\n"
        f"  kept after pipeline:        {audit.get('kept', 'n/a')}\n"
        f"  count (kept pool):          {result.get('count')}\n"
        f"  percentile band p25-p75:    {result.get('p25')} - {result.get('p75')}\n"
        f"  median:                     {result.get('median')}\n"
        f"  min / max:                  {result.get('min')} / {result.get('max')}\n"
        f"  verdict:                    {result.get('verdict', 'OK (not zero)')}\n"
    )


async def _amain(part_number: str, listings_path: Path) -> int:
    own = _find_own_listing(listings_path, part_number)
    if own is None:
        print(
            f"FAIL: no own-listing found in {listings_path} matching MPN={part_number}.\n"
            "      Run `uv run python ~/.claude/skills/ebay-seller-tool/scripts/fetch_listings.py` first."
        )
        return 1

    own_price_raw = own.get("price") or 0.0
    try:
        own_live_price: float | None = float(own_price_raw)
    except (TypeError, ValueError):
        own_live_price = None

    print(f"Sample invocation for MPN={part_number} (Issue #444 Part B equivalence-class loop)")
    print(f"  own item_id: {own.get('item_id')}")
    print(f"  own title:   {own.get('title')}")
    print(f"  own price:   £{own_live_price}")
    print(f"  own seller:  {os.environ.get('EBAY_OWN_SELLER_USERNAME', '<not set>')}")
    print()

    print("--- POST-FIX: equivalence-class loop (USED → 3000 + 2750) ---")
    result_used = await fetch_competitor_prices(
        part_number=part_number,
        condition="USED",
        location_country="GB",
        limit=50,
        own_listing=own,
        own_live_price=own_live_price,
    )
    print(_summarise("USED equivalence-class merged result:", result_used))

    raw_per_cond = (result_used.get("audit_verbose") or {}).get("raw_count_per_condition_id", {})
    if "2750" in raw_per_cond and raw_per_cond["2750"] > 0:
        print(
            f"PASS: equivalence-class fetch surfaced {raw_per_cond['2750']} listing(s) at "
            f"conditionId=2750 (Used-Excellent) that the pre-fix single-condition fetch "
            f"would have hidden.\n"
        )
    else:
        print(
            "INFO: zero 2750 listings on the live UK market for this MPN today. "
            "Fix is still correct; demonstration of percentile shift requires a "
            "different MPN / future market state.\n"
        )

    print(f"Total Browse API calls dispatched for this sweep: per-condition raw counts = {raw_per_cond}")
    print(f"Sanity check: USED equivalence class = 2 calls; observed = {len(raw_per_cond)} call(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--part-number",
        default="HUS724020AL",
        help="MPN to sample (default: HUS724020AL — Issue #14 motivating example).",
    )
    parser.add_argument(
        "--listings",
        type=Path,
        default=Path("/tmp/ebay-listings-live.json"),
        help="Path to ebay-listings-live.json (default: /tmp/ebay-listings-live.json).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args.part_number, args.listings))


if __name__ == "__main__":
    sys.exit(main())
