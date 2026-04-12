"""
ebay-seller-tool MCP server.

Provides tools for managing eBay listings from Claude Code.
Uses eBay Trading API (XML) for listing CRUD and photo uploads.
"""

import asyncio
import json
import logging
import os
import traceback
from functools import wraps

from dotenv import load_dotenv

# Load .env BEFORE importing ebay modules that read env vars
load_dotenv()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from ebay.auth import check_token_expiry, validate_credentials  # noqa: E402
from ebay.client import execute_with_retry, log_debug  # noqa: E402
from ebay.listings import (  # noqa: E402
    audit_log_write,
    build_revise_payload,
    compute_diff,
    extract_shipping_details,
    listing_to_dict,
    snapshot_listing,
)

mcp = FastMCP("ebay-seller-tool")


# Suppress ebaysdk logging unless EBAY_DEBUG=1
if not os.environ.get("EBAY_DEBUG"):
    logging.getLogger("ebaysdk").setLevel(logging.CRITICAL)

# Validate credentials at module load (runs whether invoked via __main__ or MCP framework)
validate_credentials()
check_token_expiry()


def with_error_handling(func):
    """Decorator for consistent error reporting across all MCP tools."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except SystemExit:
            raise
        except Exception as e:
            error_details = traceback.format_exc()
            log_debug(f"ERROR in {func.__name__}: {error_details}")
            return json.dumps(
                {"error": str(e), "tool": func.__name__, "details": error_details},
                indent=2,
            )

    return wrapper


@mcp.tool()
@with_error_handling
async def get_active_listings(page: int = 1, per_page: int = 25) -> str:
    """Get all active eBay listings with title, price, quantity, and watchers.

    Args:
        page: Page number (1-based). Default 1.
        per_page: Listings per page (1-200). Default 25.

    Returns:
        JSON with total count, page info, and listing details.
    """
    log_debug(f"get_active_listings page={page} per_page={per_page}")

    if page < 1:
        return json.dumps({"error": "page must be >= 1"})
    if per_page < 1 or per_page > 200:
        return json.dumps({"error": "per_page must be between 1 and 200"})

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetMyeBaySelling",
        {
            "ActiveList": {
                "Sort": "TimeLeft",
                "Pagination": {
                    "EntriesPerPage": per_page,
                    "PageNumber": page,
                },
            },
        },
    )

    if not hasattr(response.reply, "ActiveList") or response.reply.ActiveList is None:
        log_debug("get_active_listings result total=0 reason=no_active_list")
        return json.dumps({"total": 0, "page": page, "per_page": per_page, "listings": []})

    active_list = response.reply.ActiveList

    # Read the real store total from PaginationResult — even on out-of-bounds
    # pages this is set, so we don't lie with total=0 when the store actually
    # has listings on earlier pages.
    real_total = 0
    if hasattr(active_list, "PaginationResult") and active_list.PaginationResult is not None:
        try:
            real_total = int(active_list.PaginationResult.TotalNumberOfEntries)
        except (AttributeError, ValueError, TypeError):
            real_total = 0

    # Handle zero listings (ItemArray absent or empty, or Item absent).
    # This also handles out-of-bounds pages — total reflects the real store size.
    if (
        not hasattr(active_list, "ItemArray")
        or active_list.ItemArray is None
        or not hasattr(active_list.ItemArray, "Item")
        or active_list.ItemArray.Item is None
    ):
        log_debug(f"get_active_listings result total={real_total} returned=0")
        return json.dumps({"total": real_total, "page": page, "per_page": per_page, "listings": []})

    items = active_list.ItemArray.Item

    # Handle single-item response (ebaysdk returns dict not list for 1 item)
    if not isinstance(items, list):
        items = [items]

    total = real_total

    listings = []
    for item in items:
        selling_status = item.SellingStatus
        listing = {
            "item_id": str(item.ItemID),
            "title": str(item.Title),
            "price": {
                "amount": str(selling_status.CurrentPrice.value),
                "currency": str(selling_status.CurrentPrice._currencyID),
            },
            "quantity_available": int(item.QuantityAvailable),
            "watch_count": int(getattr(item, "WatchCount", 0) or 0),
            "view_count": int(getattr(item, "HitCount", 0) or 0),
            "listing_url": str(item.ListingDetails.ViewItemURL),
        }
        listings.append(listing)

    log_debug(f"get_active_listings result total={total} returned={len(listings)}")

    return json.dumps(
        {
            "total": total,
            "page": page,
            "per_page": per_page,
            "listings": listings,
        },
        indent=2,
    )


@mcp.tool()
@with_error_handling
async def get_listing_details(item_id: str) -> str:
    """Get full details for a single eBay listing.

    Returns description HTML, item specifics, photos, and all metadata.

    Args:
        item_id: The eBay item ID (numeric string).

    Returns:
        JSON with full listing details or error.
    """
    if not item_id or not item_id.strip():
        return json.dumps({"error": "item_id required"})

    log_debug(f"get_listing_details item_id={item_id}")

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll", "IncludeItemSpecifics": "true"},
    )

    if response.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})

    result = listing_to_dict(response.reply.Item)
    log_debug(
        f"get_listing_details OK item_id={item_id} title={result['title'][:50]} "
        f"desc_len={result['description_length']}"
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def update_listing(
    item_id: str,
    title: str | None = None,
    description_html: str | None = None,
    price: float | None = None,
    dry_run: bool = False,
) -> str:
    """Update title, description HTML, and/or price on an existing listing.

    This tool intentionally cannot update quantity. Quantity changes are blocked
    at input validation AND payload construction levels.

    Args:
        item_id: The eBay item ID to update.
        title: New title (max 80 chars). Optional.
        description_html: New description HTML. Optional.
        price: New price (must be > 0). Optional.
        dry_run: If True, return diff without making changes. Default False.

    Returns:
        JSON with diff (dry_run) or success result with before/after snapshots.
    """
    if not item_id or not item_id.strip():
        return json.dumps({"error": "item_id required"})
    if title is None and description_html is None and price is None:
        return json.dumps(
            {
                "error": "no fields to update — provide title, description_html, or price",
            }
        )
    if title is not None and len(title) > 80:
        return json.dumps({"error": f"title exceeds 80-char eBay limit (got {len(title)})"})
    if price is not None and price <= 0:
        return json.dumps({"error": "price must be > 0"})
    if description_html is not None:
        description_html = description_html.strip()
        if not description_html:
            return json.dumps({"error": "description_html must not be empty"})
        if len(description_html) < 50:
            return json.dumps({"error": "description_html suspiciously short (< 50 chars)"})
        if "<" not in description_html or ">" not in description_html:
            return json.dumps({"error": "description_html must contain at least one HTML tag"})

    fields = [f for f in ["title", "description_html", "price"] if locals().get(f) is not None]
    log_debug(f"update_listing item_id={item_id} dry_run={dry_run} fields={fields}")

    # Fetch current state for diff
    current = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll", "IncludeItemSpecifics": "true"},
    )
    if current.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})
    before = snapshot_listing(current.reply.Item)

    diff = compute_diff(before, title, description_html, price)

    log_debug(
        f"DIFF item_id={item_id} fields_to_change={list(diff.keys())} "
        f"before_len={before['description_length']} "
        f"after_len={len(description_html) if description_html else before['description_length']}"
    )

    if not diff:
        return json.dumps(
            {
                "item_id": item_id,
                "no_change": True,
                "message": "all fields identical",
            }
        )

    if dry_run:
        return json.dumps({"dry_run": True, "item_id": item_id, "diff": diff}, indent=2)

    # Build and send ReviseFixedPriceItem payload — echo back current shipping config
    shipping = extract_shipping_details(current.reply.Item)
    payload = build_revise_payload(item_id, title, description_html, price, shipping)
    await asyncio.to_thread(execute_with_retry, "ReviseFixedPriceItem", payload)

    # Verify by re-fetching
    after_resp = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll", "IncludeItemSpecifics": "true"},
    )
    if after_resp.reply.Item is None:
        log_debug(f"update_listing VERIFY_FAILED item_id={item_id} — item disappeared after update")
        audit_log_write(
            item_id=item_id,
            fields_changed=list(diff.keys()),
            before_length=before["description_length"],
            after_length=0,
            success=True,
            error="update applied but post-verify fetch returned empty item",
        )
        return json.dumps(
            {
                "success": True,
                "item_id": item_id,
                "fields_updated": list(diff.keys()),
                "warning": "update applied but post-verify fetch returned empty item",
            },
            indent=2,
        )
    after = snapshot_listing(after_resp.reply.Item)

    # Audit log
    audit_log_write(
        item_id=item_id,
        fields_changed=list(diff.keys()),
        before_length=before["description_length"],
        after_length=after["description_length"],
        success=True,
    )

    log_debug(f"update_listing OK item_id={item_id} fields_updated={list(diff.keys())}")

    return json.dumps(
        {
            "success": True,
            "item_id": item_id,
            "fields_updated": list(diff.keys()),
            "before": before,
            "after": after,
        },
        indent=2,
    )


if __name__ == "__main__":
    log_debug("Starting ebay-seller-tool MCP server")
    mcp.run()
