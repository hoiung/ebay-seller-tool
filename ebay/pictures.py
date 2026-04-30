"""
Listing-picture revision helper (Issue ebay-ops#10 stub #23).

`revise_pictures(item_id, photo_paths, mode='append'|'replace', confirm=False)`
is the first-class MCP-tool replacement for the bridge-script pattern (e.g.
/tmp/push_photos_<item_id>.py — proven 2026-04-28 against live listings).

Composition rule:
    append:  current PictureURL list + new EPS uploads
    replace: just new EPS uploads (DESTRUCTIVE — confirm=True required)

eBay caps PictureDetails.PictureURL at MAX_PICTURE_URLS (24). Beyond that the
listing rejects; this function warns AND truncates to the cap rather than
silently dropping the overflow. The caller is informed via the response
`truncated` / `truncated_count` fields.

ShippingDetails is echoed back via extract_shipping_details() — eBay otherwise
overwrites with default config on every Revise call. The Revise-path Quantity
invariant is preserved (build_revise_payload._assert_no_quantity).
"""

from __future__ import annotations

import asyncio

from ebay.client import execute_with_retry, log_debug
from ebay.listings import (
    MAX_PICTURE_URLS,
    audit_log_write,
    build_revise_payload,
    extract_shipping_details,
    listing_to_dict,
)
from ebay.photos import preprocess_for_ebay, upload_one


async def revise_pictures(
    item_id: str,
    photo_paths: list[str],
    mode: str = "append",
    confirm: bool = False,
    dry_run: bool = False,
) -> dict:
    """Append or replace the PictureURL list on a live listing.

    Args:
        item_id: eBay item ID.
        photo_paths: Local image paths to upload + apply.
        mode: 'append' (default) or 'replace'.
        confirm: REQUIRED when mode='replace' — guards the destructive path.
        dry_run: If True, return the composed URL plan without uploading or
            calling ReviseFixedPriceItem. Used for preview/diff.

    Returns:
        Dict with item_id, mode, photos_before, photos_after, photos_lost
        (replace-only), truncated, truncated_count, fees, live_url. Dry-run
        returns the same shape with `dry_run: True` and no API side-effects.

    Raises:
        ValueError: invalid mode, replace without confirm, empty photo_paths,
            item not found, all photos rejected by preprocessing.
    """
    if mode not in ("append", "replace"):
        raise ValueError(f"mode must be 'append' or 'replace', got {mode!r}")
    if mode == "replace" and not confirm:
        raise ValueError(
            "mode='replace' requires confirm=True (destructive — overwrites all "
            "current photos with the new set)"
        )
    if not photo_paths:
        raise ValueError("photo_paths must contain at least 1 path")

    log_debug(
        f"revise_pictures item_id={item_id} mode={mode} dry_run={dry_run} "
        f"photo_count={len(photo_paths)}"
    )

    # 1. Fetch current listing — needed for ShippingDetails echo + PictureURL diff.
    current = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {
            "ItemID": item_id,
            "DetailLevel": "ReturnAll",
            "IncludeItemSpecifics": "true",
            "IncludeWatchCount": "true",
        },
    )
    if current.reply.Item is None:
        raise ValueError(f"item {item_id} not found or no longer active")

    listing = listing_to_dict(current.reply.Item)
    photos_before = list(listing["photos"])

    # 2. Dry-run: skip uploads, project the URL plan with placeholder URLs so the
    #    caller can preview ordering + see what gets dropped in replace mode.
    if dry_run:
        placeholder_urls = [f"<dry-run-upload:{p}>" for p in photo_paths]
        composed = (
            photos_before + placeholder_urls if mode == "append" else list(placeholder_urls)
        )
        truncated = False
        truncated_count = 0
        if len(composed) > MAX_PICTURE_URLS:
            truncated_count = len(composed) - MAX_PICTURE_URLS
            composed = composed[:MAX_PICTURE_URLS]
            truncated = True
        photos_lost = list(photos_before) if mode == "replace" else []
        return {
            "dry_run": True,
            "item_id": item_id,
            "mode": mode,
            "photos_before": photos_before,
            "photos_after_preview": composed,
            "photos_lost": photos_lost,
            "photos_count_before": len(photos_before),
            "photos_count_after": len(composed),
            "truncated": truncated,
            "truncated_count": truncated_count,
            "live_url": f"https://www.ebay.co.uk/itm/{item_id}",
        }

    # 3. Upload new photos to eBay Picture Services.
    new_urls: list[str] = []
    for idx, p in enumerate(photo_paths):
        bytes_out = await asyncio.to_thread(preprocess_for_ebay, p)
        url = await asyncio.to_thread(upload_one, bytes_out)
        new_urls.append(url)
        log_debug(f"revise_pictures uploaded idx={idx} url={url}")

    # 4. Compose ordered URL list.
    if mode == "append":
        composed = photos_before + new_urls
    else:
        composed = list(new_urls)

    truncated = False
    truncated_count = 0
    if len(composed) > MAX_PICTURE_URLS:
        truncated_count = len(composed) - MAX_PICTURE_URLS
        log_debug(
            f"revise_pictures TRUNCATED item_id={item_id} mode={mode} "
            f"composed_count={len(composed)} cap={MAX_PICTURE_URLS} "
            f"dropping_last={truncated_count}"
        )
        composed = composed[:MAX_PICTURE_URLS]
        truncated = True

    # 5. Build payload — echo current ShippingDetails so eBay doesn't overwrite.
    shipping = extract_shipping_details(current.reply.Item)
    payload = build_revise_payload(
        item_id=item_id,
        shipping_details=shipping,
        picture_urls=composed,
    )

    # 6. Apply. ReviseFixedPriceItem failure post-upload leaves orphan EPS
    # photos — audit-log the orphan state so the operator can see what was
    # uploaded but never attached to a listing. Mirrors end_listing.py
    # pattern (Sonnet Ralph review finding).
    try:
        response = await asyncio.to_thread(execute_with_retry, "ReviseFixedPriceItem", payload)
    except Exception:
        audit_log_write(
            item_id=item_id,
            fields_changed=["picture_urls"],
            before_length=len(photos_before),
            after_length=len(photos_before),  # unchanged — revise failed
            success=False,
        )
        log_debug(
            f"revise_pictures FAILED post-upload item_id={item_id} "
            f"orphan_eps_urls={new_urls}"
        )
        raise
    fees = _extract_fees(response.reply)

    audit_log_write(
        item_id=item_id,
        fields_changed=["picture_urls"],
        before_length=len(photos_before),
        after_length=len(composed),
        success=True,
    )

    photos_lost = list(photos_before) if mode == "replace" else []
    return {
        "ok": True,
        "item_id": item_id,
        "mode": mode,
        "photos_before": photos_before,
        "photos_after": composed,
        "photos_lost": photos_lost,
        "photos_count_before": len(photos_before),
        "photos_count_after": len(composed),
        "truncated": truncated,
        "truncated_count": truncated_count,
        "fees": fees,
        "live_url": f"https://www.ebay.co.uk/itm/{item_id}",
    }


def _extract_fees(reply: object) -> list[dict[str, str]]:
    """Pull the Fees.Fee summary off a Trading API response."""
    fees_raw = getattr(reply, "Fees", None)
    if fees_raw is None or not hasattr(fees_raw, "Fee"):
        return []
    fee_list = fees_raw.Fee
    if not isinstance(fee_list, list):
        fee_list = [fee_list]
    return [
        {
            "name": str(f.Name),
            "fee": str(getattr(f.Fee, "value", f.Fee)),
            "currency": str(getattr(f.Fee, "_currencyID", "GBP")),
        }
        for f in fee_list
    ]
