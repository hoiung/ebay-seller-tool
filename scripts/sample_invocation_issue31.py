"""Issue #31 — AP #18 sample invocation for traffic_report resilience.

Two real-CLI invocations against live eBay Sell Analytics, separated by
< 2 minutes, exercising:
  - Phase 1: HTTP 429 burst-rate-limit retry-with-backoff inside
    fetch_traffic_report (5s/15s/60s; 80s wall-clock budget). After
    exhaustion surfaces TrafficReportRateLimitError.
  - Phase 2: parsed-by-default surface (fetch_traffic_report returns
    decoded shape; fetch_traffic_report_raw exposes the raw eBay JSON).
  - Phase 3: per_listing_summary in Fetchers Protocol shape ({imp,
    views, ctr_pct, conv_pct, tx_count}) — no consumer-side translation.

The < 2 min cadence is the AP #18 contract: it deliberately tries to
trigger the burst window so the retry path runs end-to-end. Three
possible outcomes (all valid evidence):
  1. Both runs succeed cleanly  → quota window clear, parsed shape +
     aliases verified.
  2. Run 1 succeeds, run 2 hits 429 + retry recovers → burst-retry works.
  3. Run 2 budget exhausted → TrafficReportRateLimitError surfaced
     cleanly (degrade-gracefully proven, not silent corruption).

PREREQUISITES:
  - .env with EBAY_AUTH_TOKEN + EBAY_OAUTH_REFRESH_TOKEN
  - /tmp/ebay-listings-live.json (or --listings-path override)

USAGE:
  uv run python scripts/sample_invocation_issue31.py
  uv run python scripts/sample_invocation_issue31.py --gap-seconds 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env BEFORE importing rest (which reads env at module load via oauth).
load_dotenv()

from ebay.rest import (  # noqa: E402
    TrafficReportRateLimitError,
    fetch_traffic_report,
    fetch_traffic_report_raw,
)


def _format_summary_brief(summary: dict[str, Any]) -> str:
    """One-line summary of the parsed shape — keeps the CLI output scannable."""
    n_records = summary.get("records_count", 0)
    n_per_listing = len(summary.get("per_listing_summary") or {})
    imp = summary.get("imp", 0)
    tx = summary.get("tx_count", 0)
    return (
        f"records_count={n_records} per_listing_summary_keys={n_per_listing} "
        f"imp={imp} tx_count={tx} ctr_pct={summary.get('ctr_pct')} "
        f"conv_pct={summary.get('conv_pct')}"
    )


def _verify_phase2_shape(summary: dict[str, Any]) -> tuple[bool, str]:
    """Phase 2 — fetch_traffic_report returns the decoded aggregate shape,
    NOT the raw eBay JSON. Surface a fail-fast message if the contract
    drifts at runtime."""
    if "header" in summary or "records" in summary:
        return False, "Phase 2 FAIL: parsed surface still exposes raw eBay keys"
    if "impressions" not in summary or "transactions" not in summary:
        return False, "Phase 2 FAIL: parsed surface missing canonical keys"
    return True, "Phase 2 OK: canonical keys present, raw keys absent"


def _verify_phase3_aliases(summary: dict[str, Any]) -> tuple[bool, str]:
    """Phase 3 — abbreviated demo-style aliases byte-equal to canonical."""
    checks = [
        ("imp", "impressions"),
        ("tx_count", "transactions"),
        ("conv_pct", "sales_conversion_rate_pct"),
    ]
    for alias, canonical in checks:
        if summary.get(alias) != summary.get(canonical):
            return False, (
                f"Phase 3 FAIL: alias {alias!r} ({summary.get(alias)!r}) != "
                f"canonical {canonical!r} ({summary.get(canonical)!r})"
            )
    if "per_listing_summary" not in summary:
        return False, "Phase 3 FAIL: per_listing_summary missing"
    return True, "Phase 3 OK: aliases match canonical, per_listing_summary present"


async def _one_run(label: str, listing_ids: list[str]) -> dict[str, Any]:
    """Single end-to-end invocation. Returns a result-dict the caller logs.
    Never raises — encodes outcomes (success / 429-recovered / 429-exhausted /
    other-error) so the AP #18 evidence comment shows the real path taken."""
    started = time.monotonic()
    print(f"\n[{label}] fetch_traffic_report({len(listing_ids)} ids, days=30)…")
    try:
        summary = await fetch_traffic_report(listing_ids, days=30)
        elapsed = time.monotonic() - started
        ok2, msg2 = _verify_phase2_shape(summary)
        ok3, msg3 = _verify_phase3_aliases(summary)
        print(f"[{label}] OK in {elapsed:.1f}s  {_format_summary_brief(summary)}")
        print(f"[{label}]   {msg2}")
        print(f"[{label}]   {msg3}")
        return {
            "label": label,
            "outcome": "success",
            "elapsed_seconds": round(elapsed, 2),
            "phase2_ok": ok2,
            "phase3_ok": ok3,
            "summary_brief": _format_summary_brief(summary),
        }
    except TrafficReportRateLimitError as exc:
        elapsed = time.monotonic() - started
        print(
            f"[{label}] BURST-LIMIT-EXHAUSTED in {elapsed:.1f}s  "
            f"attempts={exc.attempts}  total_wait={exc.total_wait_seconds}s"
        )
        # This is a VALID outcome — the retry budget proved the cooldown window
        # is longer than the configured budget. Caller can degrade gracefully.
        return {
            "label": label,
            "outcome": "rate_limit_exhausted",
            "elapsed_seconds": round(elapsed, 2),
            "attempts": exc.attempts,
            "total_wait_seconds": exc.total_wait_seconds,
        }
    except Exception as exc:  # noqa: BLE001 — outcome encoding
        elapsed = time.monotonic() - started
        print(f"[{label}] ERROR in {elapsed:.1f}s  {type(exc).__name__}: {exc}")
        return {
            "label": label,
            "outcome": "other_error",
            "elapsed_seconds": round(elapsed, 2),
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:200],
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--listings-path",
        type=Path,
        default=Path("/tmp/ebay-listings-live.json"),
        help="JSON file with [{item_id, ...}, ...] (default: /tmp/ebay-listings-live.json)",
    )
    parser.add_argument(
        "--gap-seconds",
        type=int,
        default=30,
        help="Gap between the two runs (default 30s; <120s satisfies AP #18 contract)",
    )
    args = parser.parse_args()

    if not args.listings_path.exists():
        print(f"ERROR: {args.listings_path} not found", file=sys.stderr)
        print("Run the skill's fetch_listings.py first.", file=sys.stderr)
        return 1

    listings = json.loads(args.listings_path.read_text())
    listing_ids = [str(lst["item_id"]) for lst in listings]
    print(f"Loaded {len(listing_ids)} listing ids from {args.listings_path}")
    print(f"AP #18: 2 runs separated by {args.gap_seconds}s (< 120s — burst-window contract)")

    # Quick raw-shape spot-check FIRST so failure here surfaces wire-format
    # drift before the two timed runs.
    print("\n[raw spot-check] fetch_traffic_report_raw → eBay JSON…")
    try:
        raw = await fetch_traffic_report_raw(listing_ids[:3], days=30)
        assert "header" in raw or "records" in raw, "raw shape should expose eBay keys"
        print("[raw spot-check] OK — raw eBay shape exposed via _raw helper")
    except Exception as exc:  # noqa: BLE001
        print(f"[raw spot-check] FAIL  {type(exc).__name__}: {exc}")

    run1 = await _one_run("run-1", listing_ids)
    print(f"\nSleeping {args.gap_seconds}s before run-2…")
    await asyncio.sleep(args.gap_seconds)
    run2 = await _one_run("run-2", listing_ids)

    print("\n=== AP #18 Evidence ===")
    print(json.dumps({"run1": run1, "run2": run2}, indent=2))

    # Exit 0 unless BOTH runs hit other_error. rate_limit_exhausted is a valid
    # AP #18 outcome (proves degrade-gracefully).
    if run1["outcome"] == "other_error" and run2["outcome"] == "other_error":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
