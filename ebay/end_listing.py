"""
End-listing helper (Issue ebay-ops#10 — gap surfaced during AP #18 sample design).

`end_listing(item_id, expected_title, ending_reason, confirm=False, dry_run=True)`
is the first-class MCP-tool wrapper around Trading API `EndFixedPriceItem`.

Designed to be safe-by-default. `EndFixedPriceItem` is destructive: once a
listing is ended, the original ItemID is gone (eBay supports relisting from
history, but with a NEW ItemID). Multiple guardrails reduce the chance of a
fat-fingered close ending the wrong listing or every listing in a loop:

  1. Single-item only — no bulk path in this module. A bulk wrapper, if ever
     needed, would have to live elsewhere and walk this function per-item.
  2. `expected_title` echo-back match — the caller MUST quote the listing's
     current live title (case-insensitive substring match). Mismatch → refuse
     loudly. This catches "wrong ItemID" mistakes before any side-effect.
  3. `confirm=True` required — guards the destructive path.
  4. Explicit `ending_reason` enum from the eBay-allowed set; no free text.
  5. `dry_run=True` is the default — caller has to explicitly opt in to the
     destructive path. Mirrors `create_listing`'s default-dry-run posture.
  6. Audit log entry per call (success or failure) via `audit_log_write`.
"""

from __future__ import annotations

import asyncio

from ebay.client import execute_with_retry, log_debug
from ebay.listings import audit_log_write, listing_to_dict

# eBay's documented EndFixedPriceItem.EndingReason enum (Trading API reference).
# Source: https://developer.ebay.com/devzone/xml/docs/Reference/eBay/EndFixedPriceItem.html
ALLOWED_ENDING_REASONS: tuple[str, ...] = (
    "NotAvailable",
    "LostOrBroken",
    "Incorrect",
    "OtherListingError",
    "SellToHighBidder",  # auction-only on eBay's side, but accepted on Revise too
)


async def end_listing(
    item_id: str,
    expected_title: str,
    ending_reason: str = "NotAvailable",
    confirm: bool = False,
    dry_run: bool = True,
) -> dict:
    """End a single live listing via Trading API EndFixedPriceItem.

    Args:
        item_id: eBay item ID.
        expected_title: caller's claimed title for this listing — checked
            case-insensitively as a substring match against the live title.
            Mismatch → refuse before any side-effect. Callers SHOULD pass the
            full live title (round-trip via get_listing_details first).
        ending_reason: one of `ALLOWED_ENDING_REASONS`. Defaults to 'NotAvailable'
            (the most common case — out-of-stock).
        confirm: REQUIRED True to actually end. Defaults False as belt-and-braces
            against accidental destructive calls.
        dry_run: True (default) returns a preview without calling EndFixedPriceItem.
            Set False to actually end.

    Returns:
        dict with keys: ok, item_id, ending_reason, dry_run, expected_title,
        live_title_pre, end_time (live only), ack (live only).

    Raises:
        ValueError: invalid ending_reason, expected_title mismatch, item not
            found, confirm not True on live path.
    """
    if ending_reason not in ALLOWED_ENDING_REASONS:
        raise ValueError(
            f"ending_reason must be one of {ALLOWED_ENDING_REASONS!r}, got {ending_reason!r}"
        )
    if not expected_title or not expected_title.strip():
        raise ValueError("expected_title is required (echo-back guard)")
    if not dry_run and not confirm:
        raise ValueError(
            "live end_listing requires confirm=True (destructive — listing "
            "cannot be un-ended; relisting requires a new ItemID)"
        )

    log_debug(
        f"end_listing item_id={item_id} reason={ending_reason} "
        f"dry_run={dry_run} confirm={confirm}"
    )

    # 1. Fetch live listing — extract title for echo-back check.
    current = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll"},
    )
    if current.reply.Item is None:
        raise ValueError(f"item {item_id} not found or no longer active")

    listing = listing_to_dict(current.reply.Item)
    live_title = listing.get("title") or ""

    # 2. Echo-back guard — case-insensitive substring match.
    if expected_title.strip().lower() not in live_title.lower():
        raise ValueError(
            f"expected_title mismatch: caller passed {expected_title!r} but "
            f"live title is {live_title!r}. Refusing to end — caller may have "
            f"the wrong item_id."
        )

    # 3. Dry-run: project the action without calling EndFixedPriceItem.
    if dry_run:
        return {
            "dry_run": True,
            "item_id": item_id,
            "ending_reason": ending_reason,
            "expected_title": expected_title,
            "live_title_pre": live_title,
            "would_end": True,
            "live_url": f"https://www.ebay.co.uk/itm/{item_id}",
        }

    # 4. Live path — call EndFixedPriceItem.
    try:
        response = await asyncio.to_thread(
            execute_with_retry,
            "EndFixedPriceItem",
            {"ItemID": item_id, "EndingReason": ending_reason},
        )
    except Exception as e:
        audit_log_write(
            item_id=item_id,
            fields_changed=["END"],
            before_length=len(live_title),
            after_length=0,
            success=False,
            condition_after=ending_reason,
        )
        raise

    ack = str(getattr(response.reply, "Ack", ""))
    end_time = str(getattr(response.reply, "EndTime", ""))

    audit_log_write(
        item_id=item_id,
        fields_changed=["END"],
        before_length=len(live_title),
        after_length=0,
        success=True,
        condition_after=ending_reason,
    )

    return {
        "ok": True,
        "item_id": item_id,
        "ending_reason": ending_reason,
        "expected_title": expected_title,
        "live_title_pre": live_title,
        "end_time": end_time,
        "ack": ack,
        "dry_run": False,
        "live_url": f"https://www.ebay.co.uk/itm/{item_id}",
    }
