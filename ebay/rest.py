"""
REST Analytics + Post-Order wrappers (Issue #4 Phase 2.3-2.5).

Uses ebay/oauth.py get_oauth_session() (user-token). Never writes to eBay —
Post-Order is STRICTLY read-only per never-dispute-customer rule.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from ebay.client import log_debug
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


def parse_traffic_report_response(traffic: dict[str, Any]) -> dict[str, Any]:
    """Positional decode of an eBay Analytics Traffic Report response.

    Real API shape (verified 2026-04-24 live probe against item 287260458724):
        traffic["header"]["metrics"][i] = {"key": "LISTING_IMPRESSION_TOTAL", ...}
        traffic["records"][i]["dimensionValues"][0] = {"value": "<listing_id>", "applicable": bool}
        traffic["records"][i]["metricValues"][i] = {"value": <num>, "applicable": bool}
            # metricValues is POSITIONAL — values are indexed by header.metrics[i].key,
            # NOT by an inline metricKey field.

    The older assumption (each metric in `rec["metrics"] = [{metricKey, value}]`) did
    not match any documented or observed response shape — it silently returned 0.

    eBay's SALES_CONVERSION_RATE and CLICK_THROUGH_RATE are decimal fractions
    (e.g. 0.06 for 6%). Converted here to the internal percentage convention so
    downstream thresholds (`compute_rank_health >= 2.0` means 2%) evaluate
    correctly.

    Returns an aggregate across all records plus per-listing breakdown:
        {
            "impressions": int (sum of LISTING_IMPRESSION_TOTAL),
            "views": int (sum of LISTING_VIEWS_TOTAL),
            "transactions": int (sum of TRANSACTION),
            "ctr_pct": float | None (computed from aggregated impressions/views),
            "sales_conversion_rate_pct": float | None (mean across records, * 100),
            "records_count": int,
            "per_listing": [{"listing_id": str | None, "metrics": {key: value, ...}}, ...],
        }

    `listing_id` is None when the record's `dimensionValues[0].value` is
    missing from the eBay response (rare; API typically always populates it).
    Downstream consumers should null-check if they key by listing_id.
    """
    header = traffic.get("header") or {}
    metric_keys = [m["key"] for m in (header.get("metrics") or [])]
    records = traffic.get("records") or []
    if not metric_keys and records:
        # Real eBay responses always include header.metrics alongside records.
        # An empty metric_keys means the API returned an unexpected shape
        # (deprecated endpoint, sandbox without data, truncated response).
        # Log loudly so operators can distinguish this from genuine zero traffic.
        log_debug(
            f"traffic_report_empty_metrics records={len(records)} "
            f"marketplace={traffic.get('filter', 'unknown')!r} — "
            f"API returned records without metrics header, aggregates will be 0"
        )

    impressions = 0
    views_total = 0
    transactions = 0
    conversions: list[float] = []
    per_listing: list[dict[str, Any]] = []

    for rec in records:
        dim_vals = rec.get("dimensionValues") or []
        # Guard against dim_vals=[{}] where value is missing — str(None) would
        # produce the string "None" and silently corrupt per_listing output.
        listing_id: str | None = None
        if dim_vals:
            raw_id = dim_vals[0].get("value")
            if raw_id is not None:
                listing_id = str(raw_id)
        mvals = rec.get("metricValues") or []
        metrics: dict[str, Any] = {}
        for i in range(min(len(metric_keys), len(mvals))):
            mv = mvals[i]
            if mv.get("applicable", True):
                metrics[metric_keys[i]] = mv.get("value")
            else:
                metrics[metric_keys[i]] = None

        try:
            impressions += int(metrics.get("LISTING_IMPRESSION_TOTAL") or 0)
            views_total += int(metrics.get("LISTING_VIEWS_TOTAL") or 0)
            transactions += int(metrics.get("TRANSACTION") or 0)
        except (TypeError, ValueError) as e:
            log_debug(
                f"traffic_report_parse_skipped listing_id={listing_id} "
                f"reason={type(e).__name__}: {e}"
            )
            continue

        scr = metrics.get("SALES_CONVERSION_RATE")
        if scr is not None:
            try:
                # eBay returns SALES_CONVERSION_RATE as a decimal fraction
                # (0.06 for 6%). Multiply by 100 to match the percentage
                # convention used by compute_rank_health thresholds.
                conversions.append(float(scr) * 100.0)
            except (ValueError, TypeError) as e:
                log_debug(
                    f"traffic_report_scr_skipped listing_id={listing_id} "
                    f"value={scr!r} reason={type(e).__name__}"
                )

        per_listing.append({"listing_id": listing_id, "metrics": metrics})

    ctr_pct = round(100.0 * views_total / impressions, 2) if impressions > 0 else None
    sales_conversion_rate_pct = (
        round(sum(conversions) / len(conversions), 2) if conversions else None
    )

    return {
        "impressions": impressions,
        "views": views_total,
        "transactions": transactions,
        "ctr_pct": ctr_pct,
        "sales_conversion_rate_pct": sales_conversion_rate_pct,
        "records_count": len(records),
        "per_listing": per_listing,
    }


def _utc_date(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _date_range(days: int) -> str:
    start = _utc_date(-days)
    end = _utc_date(0)
    # eBay expects YYYY-MM-DDTHH:MM:SSZ..YYYY-MM-DDTHH:MM:SSZ
    return f"[{start}T00:00:00.000Z..{end}T23:59:59.999Z]"


def _sync_get_traffic_report(
    listing_ids: list[str], days: int, marketplace_id: str
) -> dict[str, Any]:
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
        lst.get("quantity_sold", 0)
        for lst in sold_page["listings"]
        if lst["item_id"] == str(item_id)
    )

    # Returns via Post-Order.
    # Canonical response key is "returns". The old "members" alternative did
    # not appear in any documented or observed response shape — removed as
    # dead code. See:
    # https://developer.ebay.com/api-docs/sell/fulfillment/resources/return/methods/searchReturns
    returns_payload = await fetch_listing_returns(item_id=item_id, days=days)
    returns_list = returns_payload.get("returns", [])

    # Postage loss per return = outbound (already shipped, non-refundable)
    # + return postage (seller pays MBG).
    cfg = _load_fees_config()
    postage_per_return = float(cfg["postage"]["outbound_gbp"]) + float(cfg["postage"]["return_gbp"])

    reasons: dict[str, int] = {}
    total_refunded = 0.0
    postage_loss = 0.0
    # Post-Order v2 field-name fallbacks — docs-only verification per AC-6.2:
    # Post-Order v2 is a legacy internal eBay API with limited public docs
    # (https://developer.ebay.com/api-docs/sell/fulfillment/ covers the newer
    # Fulfillment API, not Post-Order). Without a live return in the store
    # the canonical field name cannot be confirmed via probe. Both names are
    # observed across different engagement types (seller-initiated vs
    # buyer-initiated refund). Keeping both fallbacks as defensive decode
    # until the first real return lets us diff against docs.
    for r in returns_list:
        raw_reason = r.get("reason") or r.get("returnReason")
        if raw_reason is None:
            log_debug(
                f"post_order_reason_missing item_id={item_id} "
                f"return_id={r.get('returnId') or r.get('return_id')!r} — "
                f"neither 'reason' nor 'returnReason' key present"
            )
        reason = str(raw_reason or "UNKNOWN")
        reasons[reason] = reasons.get(reason, 0) + 1
        refund = r.get("sellerTotalRefund") or r.get("buyerTotalRefund") or {}
        try:
            total_refunded += float(refund.get("value", 0.0) or 0.0)
        except (ValueError, TypeError) as e:
            log_debug(
                f"post_order_refund_parse_failed item_id={item_id} "
                f"return_id={r.get('returnId') or r.get('return_id')!r} "
                f"refund={refund!r} reason={type(e).__name__}: {e}"
            )
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
