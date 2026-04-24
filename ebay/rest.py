"""
REST Analytics + Post-Order wrappers (Issue #4 Phase 2.3-2.5).

Uses ebay/oauth.py get_oauth_session() (user-token). Never writes to eBay —
Post-Order is STRICTLY read-only per never-dispute-customer rule.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from ebay.fees import _load_fees_config
from ebay.oauth import get_oauth_session, get_post_order_session, raise_for_ebay_error

# Traffic Report metric list from research §5.
_TRAFFIC_METRICS = (
    "CLICK_THROUGH_RATE,"
    "LISTING_IMPRESSION_SEARCH_RESULTS_PAGE,"
    "LISTING_IMPRESSION_STORE,"
    "LISTING_IMPRESSION_TOTAL,"
    "LISTING_VIEWS_SOURCE_SEARCH_RESULTS_PAGE,"
    "LISTING_VIEWS_TOTAL,"
    "SALES_CONVERSION_RATE,"
    "TRANSACTION"
)


def _utc_date(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _date_range(days: int) -> str:
    start = _utc_date(-days)
    end = _utc_date(0)
    # eBay expects YYYY-MM-DDTHH:MM:SSZ..YYYY-MM-DDTHH:MM:SSZ
    return f"[{start}T00:00:00.000Z..{end}T23:59:59.999Z]"


def _sync_get_traffic_report(listing_ids: list[str], days: int, marketplace_id: str) -> dict[str, Any]:
    if not listing_ids:
        raise ValueError("listing_ids must contain at least one item")
    if days < 1 or days > 90:
        raise ValueError(f"days must be in [1, 90] per Traffic Report API; got {days}")

    filter_str = (
        f"marketplace_ids:{{{marketplace_id}}},"
        f"listing_ids:{{{'|'.join(listing_ids)}}},"
        f"date_range:{_date_range(days)}"
    )
    params = {
        "dimension": "LISTING,DAY",
        "metric": _TRAFFIC_METRICS,
        "filter": filter_str,
    }
    with get_oauth_session() as client:
        response = client.get("/sell/analytics/v1/traffic_report", params=params)
    raise_for_ebay_error(response)
    return response.json()


async def fetch_traffic_report(
    listing_ids: list[str],
    days: int = 30,
    marketplace_id: str | None = None,
) -> dict[str, Any]:
    """REST Analytics traffic_report. marketplace_id defaults to config/fees.yaml value."""
    if marketplace_id is None:
        marketplace_id = str(_load_fees_config()["ebay_uk"]["marketplace_id"])
    return await asyncio.to_thread(_sync_get_traffic_report, listing_ids, days, marketplace_id)


def _sync_get_listing_returns(item_id: str, days: int) -> dict[str, Any]:
    if not item_id or not str(item_id).strip():
        raise ValueError("item_id required")
    if days < 1 or days > 90:
        raise ValueError(f"days must be in [1, 90]; got {days}")
    creation_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    params: dict[str, Any] = {
        "item_id": str(item_id),
        "creation_date_range_from": creation_from,
        "limit": 50,
    }
    # Post-Order API rejects OAuth Bearer ('Bad scheme: Bearer' error) — uses IAF
    # with Auth'N'Auth token instead. Verified live 2026-04-24.
    with get_post_order_session() as client:
        response = client.get("/post-order/v2/return/search", params=params)
    raise_for_ebay_error(response)
    return response.json()


async def fetch_listing_returns(item_id: str, days: int = 90) -> dict[str, Any]:
    """Post-Order v2 return search for one item_id. READ-ONLY."""
    return await asyncio.to_thread(_sync_get_listing_returns, item_id, days)


async def compute_return_rate(item_id: str, days: int = 90) -> dict[str, Any]:
    """Join sold count (Phase 1 GetMyeBaySelling) with return count (Post-Order).

    Returns per-SKU rate + breakdown. If no sales in window, returns None rate.
    """
    from ebay.selling import fetch_sold_listings  # noqa: PLC0415 — avoid circular at import

    # Sold count across same window (capped to 60d per GetMyeBaySelling API).
    sold_window = min(days, 60)
    sold_page = await fetch_sold_listings(days=sold_window, per_page=200)
    units_sold = sum(
        l.get("quantity_sold", 0) for l in sold_page["listings"] if l["item_id"] == str(item_id)
    )

    # Returns via Post-Order.
    returns_payload = await fetch_listing_returns(item_id=item_id, days=days)
    returns_list = returns_payload.get("returns", []) or returns_payload.get("members", [])

    # Postage loss per return = outbound (already shipped, non-refundable) + return postage (seller pays MBG).
    cfg = _load_fees_config()
    postage_per_return = float(cfg["postage"]["outbound_gbp"]) + float(cfg["postage"]["return_gbp"])

    reasons: dict[str, int] = {}
    total_refunded = 0.0
    postage_loss = 0.0
    for r in returns_list:
        reason = str(r.get("reason") or r.get("returnReason") or "UNKNOWN")
        reasons[reason] = reasons.get(reason, 0) + 1
        refund = r.get("sellerTotalRefund") or r.get("buyerTotalRefund") or {}
        try:
            total_refunded += float(refund.get("value", 0.0) or 0.0)
        except (ValueError, TypeError):
            pass
        postage_loss += postage_per_return

    returns_opened = len(returns_list)
    rate = None
    if units_sold > 0:
        rate = round(100.0 * returns_opened / units_sold, 2)

    # Net margin impact: refunded + postage - FVF retained by eBay (fee refund of postage only)
    net_margin_impact = -(total_refunded + postage_loss)

    return {
        "item_id": str(item_id),
        "window_days": days,
        "units_sold": units_sold,
        "returns_opened": returns_opened,
        "return_rate_pct": rate,
        "return_reasons_dict": reasons,
        "total_refunded_gbp": round(total_refunded, 2),
        "estimated_postage_loss_gbp": round(postage_loss, 2),
        "net_margin_impact_gbp": round(net_margin_impact, 2),
    }
