"""
ebay-seller-tool MCP server.

Provides tools for managing eBay listings from Claude Code.
Uses eBay Trading API (XML) for listing CRUD and photo uploads.
"""

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

    response = execute_with_retry(
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


if __name__ == "__main__":
    log_debug("Starting ebay-seller-tool MCP server")
    mcp.run()
