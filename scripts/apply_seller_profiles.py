"""Issue #29 Phase 4 — one-shot Business Policies migration.

Applies the SellerProfiles block to every active listing on the store. This
is a MIGRATION script — run once after Phase 1 (browser enrolment) + Phase 2
(code switch). After this run, every listing references the three Business
Policy IDs and inline shipping/payment/returns blocks are gone.

The migration is idempotent: re-running on already-migrated listings is a
no-op (eBay's ReviseFixedPriceItem succeeds without state change when the
referenced Profile IDs are already attached).

PREREQUISITES:
  - .env populated with EBAY_AUTH_TOKEN + EBAY_OWN_SELLER_USERNAME
  - .env populated with EBAY_PAYMENT_PROFILE_ID + EBAY_SHIPPING_PROFILE_ID +
    EBAY_RETURN_PROFILE_ID (issue #29 Phase 1 enrolment output)

USAGE:
  uv run python scripts/apply_seller_profiles.py             # dry-run
  uv run python scripts/apply_seller_profiles.py --apply     # live

OUTPUT:
  Stdout: per-listing line `<item_id> <before_returns> -> <after> <status>`
  Audit log appended to ~/.local/share/ebay-seller-tool/seller_profiles_migration.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE importing ebay.* (oauth + listings read env at module load).
load_dotenv()

from ebay.client import execute_with_retry  # noqa: E402
from ebay.listings import (  # noqa: E402
    _build_seller_profiles_block,
    listing_to_dict,
)


_AUDIT_LOG = Path.home() / ".local/share/ebay-seller-tool/seller_profiles_migration.jsonl"


def _audit(record: dict) -> None:
    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    with _AUDIT_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


async def _list_active(page: int) -> tuple[list[dict], int]:
    """Return (listings, total) — 200 per page."""
    response = await asyncio.to_thread(
        execute_with_retry,
        "GetMyeBaySelling",
        {
            "ActiveList": {
                "Sort": "TimeLeft",
                "Pagination": {"EntriesPerPage": 200, "PageNumber": page},
                "IncludeWatchCount": "false",
            },
            "DetailLevel": "ReturnAll",
        },
    )
    active = getattr(response.reply, "ActiveList", None)
    if active is None:
        return [], 0
    total = 0
    if getattr(active, "PaginationResult", None) is not None:
        try:
            total = int(active.PaginationResult.TotalNumberOfEntries)
        except (AttributeError, ValueError, TypeError):
            total = 0
    items = getattr(getattr(active, "ItemArray", None), "Item", None)
    if items is None:
        return [], total
    if not isinstance(items, list):
        items = [items]
    return [listing_to_dict(it) for it in items], total


async def _fetch_all_active() -> list[dict]:
    """Page through GetMyeBaySelling.ActiveList until exhausted."""
    out: list[dict] = []
    page = 1
    while True:
        chunk, total = await _list_active(page)
        out.extend(chunk)
        if len(out) >= total or not chunk:
            return out
        page += 1


async def _apply_one(item_id: str, profiles: dict) -> dict:
    """Send ReviseFixedPriceItem with SellerProfiles only — no other field changes."""
    payload = {"Item": {"ItemID": item_id, "SellerProfiles": profiles}}
    try:
        await asyncio.to_thread(execute_with_retry, "ReviseFixedPriceItem", payload)
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _short(d: dict | None) -> str:
    if not d:
        return "?"
    accepted = d.get("returns_accepted")
    if accepted is None:
        return "?"
    if not accepted:
        return "NotAccepted"
    period = d.get("period_days")
    bp = d.get("buyer_pays")
    return f"{period}d/buyer={bp}"


async def main(apply: bool) -> int:
    # Resolve Profile IDs at startup so we Fail-Fast before any API call.
    try:
        profiles = _build_seller_profiles_block()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    print(f"Mode: {'APPLY (live)' if apply else 'DRY-RUN (no changes)'}")
    print(f"Payment={profiles['SellerPaymentProfile']['PaymentProfileID']} "
          f"Shipping={profiles['SellerShippingProfile']['ShippingProfileID']} "
          f"Return={profiles['SellerReturnProfile']['ReturnProfileID']}")

    listings = await _fetch_all_active()
    print(f"Active listings: {len(listings)}\n")

    summary = {"already_compliant": 0, "would_apply": 0, "applied": 0, "failed": 0}

    for entry in listings:
        item_id = entry["item_id"]
        before = _short(entry.get("return_policy"))

        if not apply:
            print(f"  {item_id}  before={before:<14}  [DRY-RUN — no API call]")
            summary["would_apply"] += 1
            _audit({"mode": "dry_run", "item_id": item_id, "before": before})
            continue

        result = await _apply_one(item_id, profiles)
        if result["success"]:
            print(f"  {item_id}  before={before:<14}  -> applied")
            summary["applied"] += 1
            _audit({"mode": "apply", "item_id": item_id, "before": before, "result": "ok"})
        else:
            print(f"  {item_id}  before={before:<14}  -> FAILED: {result['error']}")
            summary["failed"] += 1
            _audit(
                {
                    "mode": "apply",
                    "item_id": item_id,
                    "before": before,
                    "result": "fail",
                    "error": result["error"],
                }
            )
        # Trading API: 5000 calls/day cap. Sleep 0.5s between revises = 7200/hr ceiling
        # (well under the rate limit) and gentle on eBay's API.
        time.sleep(0.5)

    print(f"\nSummary: {summary}")
    print(f"Audit log: {_AUDIT_LOG}")
    return 0 if summary.get("failed", 0) == 0 else 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Issue #29 — apply SellerProfiles to all active listings.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Send ReviseFixedPriceItem live. Default is dry-run (no API mutation).",
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args().apply)))
