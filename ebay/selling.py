"""
Trading API read-only wrappers for analytics.

Pure extraction helpers — no MCP decoration. server.py wraps each with
@mcp.tool + @with_error_handling.

Rate limits (Trading API — daily 5,000 calls/app/day unless app-key approved):
    GetMyeBaySelling: 300 calls per 15s across all SoldList/UnsoldList/ActiveList
    GetSellerTransactions: same bucket
    GetFeedback: 5,000/day
    getUserCases (Resolution Case Management): 5,000/day
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from ebay.client import execute_with_retry
from ebay.listings import _parse_iso_ts


def _validate_window(days: int, max_days: int, tool_name: str) -> None:
    if not isinstance(days, int) or days < 1 or days > max_days:
        raise ValueError(
            f"{tool_name}: days must be int in [1, {max_days}]; got {days!r}"
        )


def _as_list(node: Any) -> list:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _days_from_to(start_str: str | None, end_str: str | None) -> int | None:
    if not start_str or not end_str:
        return None
    try:
        s = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        return max(0, (e - s).days)
    except (ValueError, TypeError):
        return None


async def fetch_sold_listings(days: int = 30, page: int = 1, per_page: int = 25) -> dict[str, Any]:
    """GetMyeBaySelling.SoldList wrapper — AC 1.3."""
    _validate_window(days, 60, "get_sold_listings")
    if per_page < 1 or per_page > 200:
        raise ValueError(f"per_page must be in [1, 200]; got {per_page}")
    if page < 1:
        raise ValueError(f"page must be >= 1; got {page}")

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetMyeBaySelling",
        {
            "SoldList": {
                "Sort": "TimeLeft",
                "DurationInDays": days,
                "Pagination": {"EntriesPerPage": per_page, "PageNumber": page},
                # Trading API: WatchCount requires explicit opt-in; DetailLevel=ReturnAll does NOT include it.
                "IncludeWatchCount": "true",
            },
        },
    )

    sold_list = getattr(response.reply, "SoldList", None)
    if sold_list is None:
        return {"total": 0, "page": page, "per_page": per_page, "listings": []}

    total = 0
    pagination = getattr(sold_list, "PaginationResult", None)
    if pagination is not None:
        try:
            total = int(pagination.TotalNumberOfEntries)
        except (AttributeError, ValueError, TypeError):
            total = 0

    orders = _as_list(getattr(getattr(sold_list, "OrderTransactionArray", None), "OrderTransaction", None))

    listings: list[dict[str, Any]] = []
    for order in orders:
        transaction = getattr(order, "Transaction", None)
        if transaction is None:
            continue
        item_node = getattr(transaction, "Item", None)
        if item_node is None:
            continue
        start_t = _parse_iso_ts(getattr(getattr(item_node, "ListingDetails", None), "StartTime", None))
        end_t = _parse_iso_ts(getattr(getattr(item_node, "ListingDetails", None), "EndTime", None))
        txn_price_obj = getattr(transaction, "TransactionPrice", None)
        txn_price = str(getattr(txn_price_obj, "value", "")) if txn_price_obj is not None else ""
        listings.append(
            {
                "item_id": str(getattr(item_node, "ItemID", "")),
                "title": str(getattr(item_node, "Title", "")),
                "sold_price": txn_price,
                "currency": str(getattr(txn_price_obj, "_currencyID", "GBP")) if txn_price_obj is not None else "GBP",
                "quantity_sold": int(getattr(transaction, "QuantityPurchased", 0) or 0),
                "start_time": start_t,
                "end_time": end_t,
                "days_live": _days_from_to(start_t, end_t),
                "best_offer_count": int(getattr(item_node, "BestOfferCount", 0) or 0),
                "watch_count": int(getattr(item_node, "WatchCount", 0) or 0),
            }
        )

    return {"total": total, "page": page, "per_page": per_page, "listings": listings}


async def fetch_unsold_listings(
    days: int = 60, page: int = 1, per_page: int = 25
) -> dict[str, Any]:
    """GetMyeBaySelling.UnsoldList wrapper — AC 1.4."""
    _validate_window(days, 60, "get_unsold_listings")
    if per_page < 1 or per_page > 200:
        raise ValueError(f"per_page must be in [1, 200]; got {per_page}")
    if page < 1:
        raise ValueError(f"page must be >= 1; got {page}")

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetMyeBaySelling",
        {
            "UnsoldList": {
                "Sort": "TimeLeft",
                "DurationInDays": days,
                "Pagination": {"EntriesPerPage": per_page, "PageNumber": page},
                # Trading API: WatchCount requires explicit opt-in; DetailLevel=ReturnAll does NOT include it.
                "IncludeWatchCount": "true",
            },
        },
    )

    unsold_list = getattr(response.reply, "UnsoldList", None)
    if unsold_list is None:
        return {"total": 0, "page": page, "per_page": per_page, "listings": []}

    total = 0
    pagination = getattr(unsold_list, "PaginationResult", None)
    if pagination is not None:
        try:
            total = int(pagination.TotalNumberOfEntries)
        except (AttributeError, ValueError, TypeError):
            total = 0

    items = _as_list(getattr(getattr(unsold_list, "ItemArray", None), "Item", None))

    listings: list[dict[str, Any]] = []
    for item_node in items:
        start_t = _parse_iso_ts(getattr(getattr(item_node, "ListingDetails", None), "StartTime", None))
        end_t = _parse_iso_ts(getattr(getattr(item_node, "ListingDetails", None), "EndTime", None))
        price_obj = getattr(getattr(item_node, "SellingStatus", None), "CurrentPrice", None)
        listings.append(
            {
                "item_id": str(getattr(item_node, "ItemID", "")),
                "title": str(getattr(item_node, "Title", "")),
                "price": str(getattr(price_obj, "value", "")) if price_obj is not None else "",
                "currency": str(getattr(price_obj, "_currencyID", "GBP")) if price_obj is not None else "GBP",
                "quantity_sold": 0,
                "start_time": start_t,
                "end_time": end_t,
                "days_live": _days_from_to(start_t, end_t),
                "best_offer_count": int(getattr(item_node, "BestOfferCount", 0) or 0),
                "watch_count": int(getattr(item_node, "WatchCount", 0) or 0),
            }
        )

    return {"total": total, "page": page, "per_page": per_page, "listings": listings}


async def fetch_seller_transactions(days: int = 30, page: int = 1) -> dict[str, Any]:
    """GetSellerTransactions wrapper — AC 1.5.

    Max 30-day window per call, 90-day lookback per API spec.
    """
    _validate_window(days, 30, "get_seller_transactions")
    if page < 1:
        raise ValueError(f"page must be >= 1; got {page}")

    # eBay Trading API expects millisecond precision: YYYY-MM-DDTHH:MM:SS.sssZ
    _ebay_ts_fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    now = datetime.now(timezone.utc)
    mod_time_to = now.strftime(_ebay_ts_fmt)
    mod_time_from = (now - timedelta(days=days)).strftime(_ebay_ts_fmt)

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetSellerTransactions",
        {
            "ModTimeFrom": mod_time_from,
            "ModTimeTo": mod_time_to,
            "Pagination": {"EntriesPerPage": 100, "PageNumber": page},
            "DetailLevel": "ReturnAll",
        },
    )

    txns_node = getattr(response.reply, "TransactionArray", None)
    transactions = _as_list(getattr(txns_node, "Transaction", None)) if txns_node is not None else []

    results: list[dict[str, Any]] = []
    for t in transactions:
        item_node = getattr(t, "Item", None)
        price_obj = getattr(t, "TransactionPrice", None)
        created = _parse_iso_ts(getattr(t, "CreatedDate", None))
        paid = _parse_iso_ts(getattr(t, "PaidTime", None))
        shipped = _parse_iso_ts(getattr(t, "ShippedTime", None))
        start_listing = _parse_iso_ts(
            getattr(getattr(item_node, "ListingDetails", None), "StartTime", None) if item_node else None
        )
        results.append(
            {
                "transaction_id": str(getattr(t, "TransactionID", "")),
                "item_id": str(getattr(item_node, "ItemID", "")) if item_node else "",
                "created_date": created,
                "paid_time": paid,
                "shipped_time": shipped,
                "transaction_price": str(getattr(price_obj, "value", "")) if price_obj is not None else "",
                "currency": str(getattr(price_obj, "_currencyID", "GBP")) if price_obj is not None else "GBP",
                "quantity_purchased": int(getattr(t, "QuantityPurchased", 0) or 0),
                "days_to_sell": _days_from_to(start_listing, created),
            }
        )
    return {"page": page, "window_days": days, "transactions": results}


async def fetch_listing_feedback(item_id: str, days: int = 90) -> dict[str, Any]:
    """GetFeedback(ItemID=X) wrapper — AC 1.6.

    Returns per-transaction feedback + aggregated DSR (4 dimensions).
    Window is client-side filter — GetFeedback returns all feedback for the item.
    """
    if not item_id or not str(item_id).strip():
        raise ValueError("item_id required")
    _validate_window(days, 3650, "get_listing_feedback")

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetFeedback",
        {
            "ItemID": str(item_id),
            "DetailLevel": "ReturnAll",
        },
    )

    feedback_details = _as_list(getattr(response.reply, "FeedbackDetailArray", None))
    if feedback_details and hasattr(feedback_details[0], "FeedbackDetail"):
        feedback_details = _as_list(feedback_details[0].FeedbackDetail)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    entries: list[dict[str, Any]] = []
    dsr_item_as_described: list[float] = []

    for fb in feedback_details:
        comment_time_str = _parse_iso_ts(getattr(fb, "CommentTime", None))
        # Window filter — skip if outside requested window.
        if comment_time_str is not None:
            try:
                ct = datetime.fromisoformat(comment_time_str.replace("Z", "+00:00"))
                if ct < cutoff:
                    continue
            except (ValueError, TypeError):
                pass

        dsr: dict[str, float] = {}
        dsr_node = getattr(fb, "SellerDSR", None) or getattr(fb, "FeedbackRatingDetailArray", None)
        # DSR may also be flat — best-effort extract
        for attr in ("ItemAsDescribed", "CommunicationRating", "ShippingTimeRating", "ShippingAndHandlingCharges"):
            v = getattr(fb, attr, None)
            if v is not None:
                try:
                    dsr[attr] = float(v)
                except (ValueError, TypeError):
                    pass
        iad = dsr.get("ItemAsDescribed")
        if iad is not None:
            dsr_item_as_described.append(iad)

        entries.append(
            {
                "commenting_user": str(getattr(fb, "CommentingUser", "")),
                "comment_text": str(getattr(fb, "CommentText", "")),
                "comment_time": comment_time_str,
                "comment_type": str(getattr(fb, "CommentType", "")),
                "dsr_ratings": dsr,
                "dsr_item_as_described": iad,
            }
        )

    dsr_avg = round(sum(dsr_item_as_described) / len(dsr_item_as_described), 2) if dsr_item_as_described else None

    return {
        "item_id": str(item_id),
        "window_days": days,
        "feedback_count": len(entries),
        "dsr_item_as_described_avg": dsr_avg,
        "entries": entries,
    }


async def fetch_listing_cases(item_id: str, days: int = 90) -> dict[str, Any]:
    """getUserCases wrapper — AC 1.7.

    Uses Resolution Case Management API. Filter on EBP_INR (item not received)
    + EBP_SNAD (significantly not as described).
    """
    if not item_id or not str(item_id).strip():
        raise ValueError("item_id required")
    _validate_window(days, 90, "get_listing_cases")

    response = await asyncio.to_thread(
        execute_with_retry,
        "getUserCases",
        {
            "ItemID": str(item_id),
            "CaseTypeFilter": {"CaseTypeArray": {"CaseType": ["EBP_INR", "EBP_SNAD"]}},
            "CaseStatusFilter": {"CaseStatusArray": {"CaseStatus": ["OPEN", "CLOSED"]}},
        },
    )

    cases_node = getattr(response.reply, "CaseArray", None)
    cases = _as_list(getattr(cases_node, "Case", None)) if cases_node is not None else []

    results: list[dict[str, Any]] = []
    for c in cases:
        results.append(
            {
                "case_id": str(getattr(getattr(c, "CaseID", None), "Value", getattr(c, "CaseID", ""))),
                "case_type": str(getattr(c, "CaseType", "")),
                "case_status": str(getattr(c, "CaseStatus", "")),
                "creation_date": _parse_iso_ts(getattr(c, "CreationDate", None)),
                "transaction_id": str(getattr(c, "TransactionID", "")),
            }
        )
    return {
        "item_id": str(item_id),
        "window_days": days,
        "open_cases": sum(1 for r in results if r["case_status"] == "OPEN"),
        "total_cases": len(results),
        "cases": results,
    }
