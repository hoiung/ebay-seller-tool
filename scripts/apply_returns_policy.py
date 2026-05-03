"""Issue #29 Phase 4 — one-shot Business Policies migration.

Applies the SellerProfiles block to every active listing on the store. This
is a MIGRATION script — run once after Phase 1 (browser enrolment) + Phase 2
(code switch). After this run, every listing references the three Business
Policy IDs and inline shipping/payment/returns blocks are gone.

The migration is idempotent: re-running on already-migrated listings detects
the target state via per-listing GetItem and skips without an API mutation.

PREREQUISITES:
  - .env populated with EBAY_AUTH_TOKEN + EBAY_OWN_SELLER_USERNAME
  - .env populated with EBAY_PAYMENT_PROFILE_ID + EBAY_SHIPPING_PROFILE_ID +
    EBAY_RETURN_PROFILE_ID (issue #29 Phase 1 enrolment output)

USAGE:
  uv run python scripts/apply_returns_policy.py             # dry-run
  uv run python scripts/apply_returns_policy.py --apply     # live

OUTPUT:
  Stdout: per-listing line with before + after policy + status
  Audit log appended to ~/.local/share/ebay-seller-tool/seller_profiles_migration.jsonl
  with full {item_id, before, after, timestamp, result} per the issue Phase 4 AC.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    build_revise_payload,
    listing_to_dict,
)


_AUDIT_LOG = Path.home() / ".local/share/ebay-seller-tool/seller_profiles_migration.jsonl"


def _audit(record: dict) -> None:
    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    with _AUDIT_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


async def _list_active_ids(page: int) -> tuple[list[str], int]:
    """Return (item_ids, total) for one page of GetMyeBaySelling.ActiveList."""
    response = await asyncio.to_thread(
        execute_with_retry,
        "GetMyeBaySelling",
        {
            "ActiveList": {
                "Sort": "TimeLeft",
                "Pagination": {"EntriesPerPage": 200, "PageNumber": page},
                "IncludeWatchCount": "false",
            },
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
    return [str(it.ItemID) for it in items], total


async def _fetch_all_active_ids() -> list[str]:
    """Page through GetMyeBaySelling.ActiveList until exhausted."""
    out: list[str] = []
    page = 1
    while True:
        chunk, total = await _list_active_ids(page)
        out.extend(chunk)
        if len(out) >= total or not chunk:
            return out
        page += 1


async def _get_item_full(item_id: str) -> dict:
    """GetItem with ReturnAll detail — used to capture before/after policy state."""
    response = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll", "IncludeItemSpecifics": "false"},
    )
    return listing_to_dict(response.reply.Item)


async def _apply_one(item_id: str) -> dict:
    """ReviseFixedPriceItem via build_revise_payload (preserves _assert_no_quantity safety).

    No other field changes — build_revise_payload with item_id only emits
    {ItemID, SellerProfiles} plus the codified Quantity-absence invariant.
    """
    payload = build_revise_payload(item_id=item_id)
    try:
        await asyncio.to_thread(execute_with_retry, "ReviseFixedPriceItem", payload)
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _short(d: dict | None) -> str:
    if not d:
        return "no_policy"
    accepted = d.get("returns_accepted")
    if accepted is None:
        return "no_policy"
    if not accepted:
        return "NotAccepted"
    period = d.get("period_days")
    bp = d.get("buyer_pays")
    return f"{period}d/buyer={bp}"


def _is_compliant(policy: dict | None) -> bool:
    """Already at target state (30d / buyer_pays / accepted) → no need to apply."""
    if not policy:
        return False
    return (
        policy.get("returns_accepted") is True
        and policy.get("period_days") == 30
        and policy.get("buyer_pays") is True
    )


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

    item_ids = await _fetch_all_active_ids()
    print(f"Active listings: {len(item_ids)}\n")

    summary = {"already_compliant": 0, "applied": 0, "failed": 0, "skipped_non_fixed": 0}

    for item_id in item_ids:
        # Per-listing GetItem to capture true before-state (full policy fields).
        try:
            before_full = await _get_item_full(item_id)
        except Exception as exc:
            print(f"  {item_id}  ERROR fetching before-state: {type(exc).__name__}: {exc}")
            summary["failed"] += 1
            _audit({"mode": "apply" if apply else "dry_run", "item_id": item_id,
                    "result": "fail", "error": f"GetItem: {exc}"})
            continue

        # Phase 4 AC: only revise FixedPriceItem listings — auction-format
        # listings would 400 on ReviseFixedPriceItem.
        listing_type = before_full.get("listing_type")
        if listing_type and listing_type not in ("FixedPriceItem", "StoresFixedPrice"):
            print(f"  {item_id}  SKIP listing_type={listing_type}")
            summary["skipped_non_fixed"] += 1
            _audit({"mode": "apply" if apply else "dry_run", "item_id": item_id,
                    "result": "skip", "reason": f"listing_type={listing_type}"})
            continue

        before_policy = before_full.get("return_policy")
        before = _short(before_policy)

        # Idempotency: skip if already at target state.
        if _is_compliant(before_policy):
            print(f"  {item_id}  before={before:<20}  -> already_compliant (skipped)")
            summary["already_compliant"] += 1
            _audit({"mode": "apply" if apply else "dry_run", "item_id": item_id,
                    "before": before_policy, "result": "already_compliant"})
            continue

        if not apply:
            print(f"  {item_id}  before={before:<20}  [DRY-RUN — would apply]")
            _audit({"mode": "dry_run", "item_id": item_id, "before": before_policy})
            continue

        result = await _apply_one(item_id)
        if not result["success"]:
            print(f"  {item_id}  before={before:<20}  -> FAILED: {result['error']}")
            summary["failed"] += 1
            _audit({"mode": "apply", "item_id": item_id, "before": before_policy,
                    "result": "fail", "error": result["error"]})
            continue

        # Verify by re-fetching post-revise — confirm policy actually flipped.
        try:
            after_full = await _get_item_full(item_id)
            after_policy = after_full.get("return_policy")
            after = _short(after_policy)
        except Exception as exc:
            print(f"  {item_id}  before={before:<20}  -> applied (verify failed: {exc})")
            summary["applied"] += 1
            _audit({"mode": "apply", "item_id": item_id, "before": before_policy,
                    "result": "applied_verify_failed", "error": str(exc)})
            continue

        verified = _is_compliant(after_policy)
        status = "applied + verified" if verified else "applied but state mismatch"
        print(f"  {item_id}  before={before:<20}  after={after:<20}  -> {status}")
        summary["applied" if verified else "failed"] += 1
        _audit({
            "mode": "apply", "item_id": item_id,
            "before": before_policy, "after": after_policy,
            "result": "verified" if verified else "applied_state_mismatch",
        })

        # Trading API: 5000 calls/day cap. Sleep 0.5s between revises = 7200/hr ceiling.
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
