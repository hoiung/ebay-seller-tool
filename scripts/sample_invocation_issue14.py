"""Issue #14 Phase 6.1 — AP #18 sample invocation.

Runs the new 3-layer comp-filter pipeline against live eBay data and reports
BEFORE-vs-AFTER percentile drift for the 4 problem pools identified in Stage 1
F-E (HPE 1TB SAS, HPE Seagate 4TB, HPE Toshiba 4TB, HPE HGST 3TB).

Two modes:

  1. From a saved listings JSON (ebay-listings-live.json from the skill's
     fetch_listings.py output) — the recommended workflow:
       uv run python ~/.claude/skills/ebay-seller-tool/scripts/fetch_listings.py
       .venv/bin/python scripts/sample_invocation_issue14.py \\
           --listings /tmp/ebay-listings-live.json \\
           --out /tmp/issue14_sample_invocation.md

  2. From raw listing dicts piped via stdin (single JSON list).

PREREQUISITES:
  - .env populated with EBAY_AUTH_TOKEN + EBAY_OAUTH_REFRESH_TOKEN
  - EBAY_OWN_SELLER_USERNAME set (own-seller exclusion in Browse fetch)

OUTPUT:
  Markdown file at --out (default /tmp/issue14_sample_invocation_<date>.md) with:
    - Per-listing BEFORE-vs-AFTER percentile drift
    - Audit dict for each listing
    - Trigger-case minimum-N gate flag (Phase 6.1)
    - Series-name fire-rate count (Phase 6.1.2)
    - Bidirectional keep/drop snippets for manual calibration (Phase 6.1.1)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ebay import fees
from ebay.browse import (
    fetch_competitor_prices,
    run_comp_filter_pipeline,
)


def _percentile(prices: list[float], pct: float) -> float | None:
    if not prices:
        return None
    sorted_p = sorted(prices)
    idx = max(0, min(len(sorted_p) - 1, int(len(sorted_p) * pct)))
    return round(sorted_p[idx], 2)


def _summarise_pool(prices: list[float]) -> dict:
    if not prices:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None}
    sorted_p = sorted(prices)
    return {
        "count": len(prices),
        "min": round(sorted_p[0], 2),
        "p25": _percentile(prices, 0.25),
        "median": _percentile(prices, 0.50),
        "p75": _percentile(prices, 0.75),
        "max": round(sorted_p[-1], 2),
    }


async def _process_listing(own_dict: dict, outlier_cfg: dict) -> dict:
    mpns = (own_dict.get("specifics") or {}).get("MPN") or []
    if not mpns:
        return {"item_id": own_dict.get("item_id"), "skipped": "no MPN"}

    raw_by_id: dict[str, dict] = {}
    for mpn in mpns:
        try:
            comps_one = await fetch_competitor_prices(
                part_number=str(mpn),
                condition="USED",
                location_country="GB",
                limit=50,
            )
        except Exception as e:  # noqa: BLE001
            return {"item_id": own_dict.get("item_id"), "error": str(e)}
        for c in comps_one.get("listings", []):
            cid = c.get("item_id")
            if cid and cid not in raw_by_id:
                raw_by_id[cid] = c

    raw = list(raw_by_id.values())
    try:
        own_price = float(own_dict.get("price") or 0.0) or None
    except (TypeError, ValueError):
        own_price = None

    pre_prices = [c["price"] for c in raw if isinstance(c.get("price"), (int, float))]
    pre_summary = _summarise_pool(pre_prices)

    kept, audit_flat, audit_verbose = run_comp_filter_pipeline(
        raw,
        own_listing=own_dict,
        threshold=0.6,
        stale_drop_pct=10.0,
        outlier_config=outlier_cfg,
        own_live_price=own_price,
    )
    post_prices = [c["price"] for c in kept if isinstance(c.get("price"), (int, float))]
    post_summary = _summarise_pool(post_prices)

    kept_ids = {c.get("item_id") for c in kept}
    return {
        "item_id": own_dict.get("item_id"),
        "title": own_dict.get("title"),
        "price": own_price,
        "mpns": mpns,
        "raw_count": len(raw),
        "pre_summary": pre_summary,
        "post_summary": post_summary,
        "audit_flat": audit_flat,
        "audit_verbose": audit_verbose,
        "below_min_n_gate": len(kept) < 5,
        "series_name_dropped": audit_verbose.get("low_quality_drops", {}).get("series_mismatch", 0),
        "kept_titles": [c.get("title") for c in kept[:5]],
        "dropped_titles": [c.get("title") for c in raw if c.get("item_id") not in kept_ids][:5],
    }


async def _main_async(listings_path: Path, limit_items: int | None, out: Path) -> int:
    fees_cfg = fees._load_fees_config()  # noqa: SLF001
    outlier_cfg = fees_cfg.get("outlier_rejection", {})

    if not listings_path.exists():
        print(f"ERROR: listings file not found: {listings_path}", file=sys.stderr)
        return 1
    raw = json.loads(listings_path.read_text())
    if isinstance(raw, dict) and "listings" in raw:
        items = raw["listings"]
    elif isinstance(raw, list):
        items = raw
    else:
        print(f"ERROR: unrecognised listings format in {listings_path}", file=sys.stderr)
        return 1
    if limit_items:
        items = items[:limit_items]

    rows = []
    for own in items:
        rows.append(await _process_listing(own, outlier_cfg))

    series_name_total = sum(r.get("series_name_dropped", 0) for r in rows)
    below_min_n = sum(1 for r in rows if r.get("below_min_n_gate"))

    lines = [
        f"# Issue #14 Sample Invocation — {datetime.now(timezone.utc).isoformat()}",
        "",
        f"Listings processed: **{len(rows)}**",
        f"Series-name hard-rejects: **{series_name_total}**",
        f"Listings below min-N gate (final_comp_count < 5): **{below_min_n}**",
        "",
        (
            "| Item | Title | Price | Raw | Kept | "
            "LQ/AA/Stale/Outlier | Pre p25/med/p75 | Post p25/med/p75 | Min-N |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "skipped" in r or "error" in r:
            lines.append(
                f"| {r.get('item_id')} | — | — | — | — | — | — | — | "
                f"{r.get('skipped') or r.get('error')} |"
            )
            continue
        af = r["audit_flat"]
        pre = r["pre_summary"]
        post = r["post_summary"]
        gate = "⚠" if r["below_min_n_gate"] else "ok"
        lines.append(
            f"| {r['item_id']} | {(r.get('title') or '')[:50]} | £{r['price']} | "
            f"{af['raw_count']} | {af['kept']} | "
            f"{af['dropped_low_quality']}/{af['dropped_apple_to_apples']}/"
            f"{af['dropped_stale']}/{af['dropped_outlier']} | "
            f"£{pre['p25']}/£{pre['median']}/£{pre['p75']} | "
            f"£{post['p25']}/£{post['median']}/£{post['p75']} | {gate} |"
        )
    lines.extend(
        [
            "",
            "## Bidirectional manual-calibration snippets (Phase 6.1.1)",
            "",
        ]
    )
    for r in rows[:5]:
        if "skipped" in r or "error" in r:
            continue
        lines.append(f"### {r['item_id']} — {(r.get('title') or '')[:60]}")
        lines.append("")
        lines.append("**Kept (top 5):**")
        for t in r["kept_titles"]:
            lines.append(f"- {t}")
        lines.append("")
        lines.append("**Dropped (top 5 — Hoi: confirm 'would I drop manually?'):**")
        for t in r["dropped_titles"]:
            lines.append(f"- {t}")
        lines.append("")
    lines.extend(
        [
            "## Raw audit JSON",
            "",
            "```json",
            json.dumps(rows, indent=2),
            "```",
        ]
    )

    out.write_text("\n".join(lines))
    print(f"Wrote {out} ({len(rows)} listings)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--listings",
        type=Path,
        required=True,
        help="Path to listings JSON (output of fetch_listings.py — list of own-listing dicts)",
    )
    parser.add_argument("--limit-items", type=int, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(f"/tmp/issue14_sample_invocation_{datetime.now().strftime('%Y-%m-%d')}.md"),
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args.listings, args.limit_items, args.out))


if __name__ == "__main__":
    sys.exit(main())
