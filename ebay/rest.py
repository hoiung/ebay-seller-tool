"""
REST Analytics + Post-Order wrappers (Issue #4 Phase 2.3-2.5).

Uses ebay/oauth.py get_oauth_session() (user-token). Never writes to eBay —
Post-Order is STRICTLY read-only per never-dispute-customer rule.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ebay.client import log_debug
from ebay.fees import _load_fees_config
from ebay.oauth import get_oauth_session, get_post_order_session, raise_for_ebay_error


class TrafficReportRateLimitError(RuntimeError):
    """Raised by ``fetch_traffic_report`` after the burst-rate-limit retry
    budget is exhausted (Issue #31 Phase 1).

    Distinct from ``call_accountant.RateLimitError`` (which is the daily
    quota gate, raised before any network round-trip). This error means
    eBay returned HTTP 429 and the configured retry sequence did not
    recover within ``total_wait_seconds``.

    Carries enough metadata for callers to choose between fail-loud and
    degrade-gracefully without re-parsing log lines:
        attempts: int — total HTTP attempts made (>=1)
        total_wait_seconds: float — wall-clock time spent in retry sleeps
        last_error: str — the last upstream error message
    """

    def __init__(self, *, attempts: int, total_wait_seconds: float, last_error: str) -> None:
        self.attempts = attempts
        self.total_wait_seconds = total_wait_seconds
        self.last_error = last_error
        super().__init__(
            f"traffic_report rate-limited: {attempts} attempts, "
            f"waited {total_wait_seconds:.1f}s, last_error={last_error!r}"
        )


# Issue #31 Phase 1 — burst-window retry. Schedule chosen empirically:
# observed cooldown >5min, so the first retry waits 5s (most opportunistic
# recovery), the second 15s, the third 60s. Total wall-clock budget 80s
# keeps the orchestrator responsive while giving any short-window throttle
# a real chance to clear. Callers needing a longer budget pass overrides.
_BURST_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0, 60.0)
_BURST_RETRY_TOTAL_BUDGET_SECONDS: float = 80.0
_RATE_LIMITED_MESSAGE_RE = re.compile(r"^eBay API 429\b")

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

    Real API shape (verified 2026-04-24 live probe against a real listing;
    fixture redacted to synthetic ID 999000000001 in tests/fixtures/):
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
            "search_impression_share_pct": float | None
                (LISTING_IMPRESSION_SEARCH_RESULTS_PAGE / LISTING_IMPRESSION_TOTAL),
            "store_impression_share_pct": float | None
                (LISTING_IMPRESSION_STORE / LISTING_IMPRESSION_TOTAL),
            "search_view_share_pct": float | None
                (LISTING_VIEWS_SOURCE_SEARCH_RESULTS_PAGE / LISTING_VIEWS_TOTAL),
            "organic_search_exposure_pct": float | None
                (synonym of search_impression_share_pct, retained for callers
                 that read the search-vs-paid framing rather than the funnel),
            "records_count": int,
            "per_listing": [{"listing_id": str | None, "metrics": {key: value, ...}}, ...],

            # Issue #31 Phase 3 — abbreviated demo-style aliases for the
            # ebay-ops Fetchers Protocol contract ({imp, views, ctr_pct,
            # conv_pct, tx_count}). One-way: server-side parse is canonical.
            "imp": int (alias of impressions),
            "tx_count": int (alias of transactions),
            "conv_pct": float | None (alias of sales_conversion_rate_pct),
            "per_listing_summary": {
                listing_id: {
                    "imp": int, "views": int, "tx_count": int,
                    "ctr_pct": float | None, "conv_pct": float | None,
                }
            } — pre-aggregated across days per listing, abbreviated keys.
            Records with listing_id=None are omitted from this dict.
        }

    Note on CLICK_THROUGH_RATE: the API-provided CTR field is unreliable
    (1% reported vs 2.19% computed from impressions/views on the redacted
    real-fixture probe — a ~2x divergence). We ignore the raw API CTR
    and surface our own `ctr_pct` computed from the aggregated impression
    + view counts. The API value is intentionally NOT exposed in the
    return shape.

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
    search_impressions = 0
    store_impressions = 0
    search_views = 0
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
            search_impressions += int(metrics.get("LISTING_IMPRESSION_SEARCH_RESULTS_PAGE") or 0)
            store_impressions += int(metrics.get("LISTING_IMPRESSION_STORE") or 0)
            search_views += int(metrics.get("LISTING_VIEWS_SOURCE_SEARCH_RESULTS_PAGE") or 0)
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
    search_impression_share_pct = (
        round(100.0 * search_impressions / impressions, 2) if impressions > 0 else None
    )
    store_impression_share_pct = (
        round(100.0 * store_impressions / impressions, 2) if impressions > 0 else None
    )
    search_view_share_pct = (
        round(100.0 * search_views / views_total, 2) if views_total > 0 else None
    )
    # Synonym of search_impression_share_pct — surfaced under the search-vs-paid
    # framing (organic = unpaid search-results-page exposure).
    organic_search_exposure_pct = search_impression_share_pct

    # Issue #31 Phase 3 — per-listing aggregate in demo-style abbreviated keys.
    # The eBay query dimension is LISTING,DAY so the parser sees one record
    # per (listing × day); this rolls them up per listing_id so the consumer
    # (orchestrator Fetchers Protocol) does not redo the math. Records with
    # listing_id=None are omitted — a None key in the summary dict would let
    # an unrelated record overwrite a real listing's totals if id-coercion
    # logic upstream changed.
    per_listing_summary: dict[str, dict[str, Any]] = {}
    for rec in per_listing:
        lid = rec.get("listing_id")
        if not lid:
            continue
        m = rec.get("metrics") or {}
        agg = per_listing_summary.setdefault(
            lid,
            {"imp": 0, "views": 0, "tx_count": 0, "_conv_sum": 0.0, "_conv_n": 0},
        )
        try:
            agg["imp"] += int(m.get("LISTING_IMPRESSION_TOTAL") or 0)
            agg["views"] += int(m.get("LISTING_VIEWS_TOTAL") or 0)
            agg["tx_count"] += int(m.get("TRANSACTION") or 0)
        except (TypeError, ValueError):
            pass
        scr_v = m.get("SALES_CONVERSION_RATE")
        if scr_v is not None:
            try:
                agg["_conv_sum"] += float(scr_v) * 100.0
                agg["_conv_n"] += 1
            except (ValueError, TypeError):
                pass
    for lid, agg in per_listing_summary.items():
        # CTR computed from impressions+views (the API-provided CTR is
        # unreliable per the parse docstring); SCR averaged across days.
        agg["ctr_pct"] = (
            round(100.0 * agg["views"] / agg["imp"], 2) if agg["imp"] > 0 else None
        )
        agg["conv_pct"] = (
            round(agg["_conv_sum"] / agg["_conv_n"], 2) if agg["_conv_n"] > 0 else None
        )
        del agg["_conv_sum"]
        del agg["_conv_n"]

    return {
        # Canonical descriptive names.
        "impressions": impressions,
        "views": views_total,
        "transactions": transactions,
        "ctr_pct": ctr_pct,
        "sales_conversion_rate_pct": sales_conversion_rate_pct,
        "search_impression_share_pct": search_impression_share_pct,
        "store_impression_share_pct": store_impression_share_pct,
        "search_view_share_pct": search_view_share_pct,
        "organic_search_exposure_pct": organic_search_exposure_pct,
        "records_count": len(records),
        "per_listing": per_listing,
        # Issue #31 Phase 3 — abbreviated demo-style aliases for the
        # ebay-ops Fetchers Protocol. One-way: server-side parse is the
        # canonical emitter; consumers no longer hand-translate.
        "imp": impressions,
        "tx_count": transactions,
        "conv_pct": sales_conversion_rate_pct,
        "per_listing_summary": per_listing_summary,
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


def _is_rate_limited_error(exc: BaseException) -> bool:
    """Issue #31 — detect HTTP 429 surfaced via ``oauth.raise_for_ebay_error``.

    The oauth helper raises ``PermissionError("eBay API 429 on <url>: <body>")``
    for any 4xx/5xx; we discriminate by status-code prefix so 401/403/5xx do
    NOT trigger the burst-retry path (those need different recovery — token
    refresh, fail-loud, etc).
    """
    if not isinstance(exc, PermissionError):
        return False
    return bool(_RATE_LIMITED_MESSAGE_RE.match(str(exc)))


def _sync_get_traffic_report_with_retry(
    listing_ids: list[str],
    days: int,
    marketplace_id: str,
    *,
    backoff_seconds: tuple[float, ...] = _BURST_RETRY_BACKOFF_SECONDS,
    total_budget_seconds: float = _BURST_RETRY_TOTAL_BUDGET_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Issue #31 Phase 1 — wrap ``_sync_get_traffic_report`` with bounded
    retry-with-backoff for HTTP 429 burst-rate-limit responses.

    Other errors (4xx non-429, 5xx, network drop, parse failures) propagate
    on the first occurrence — burst-retry is opt-in for the specific 429
    failure mode and must not mask different root causes.

    ``sleep_fn`` and ``monotonic_fn`` are injected so tests can drive the
    retry loop without burning real seconds. Production callers always use
    the defaults.

    Raises:
        TrafficReportRateLimitError: once ``backoff_seconds`` and
            ``total_budget_seconds`` are exhausted on consecutive 429s.
        Exception: any non-429 upstream error, raised on the first attempt.
    """
    deadline = monotonic_fn() + total_budget_seconds
    attempts = 0
    total_wait = 0.0
    last_error_message = ""
    for backoff in (None,) + tuple(backoff_seconds):
        if backoff is not None:
            remaining_budget = deadline - monotonic_fn()
            if remaining_budget <= 0:
                break
            wait_seconds = min(float(backoff), remaining_budget)
            log_debug(
                f"fetch_traffic_report 429_RETRY attempt={attempts + 1} "
                f"wait_seconds={wait_seconds:.1f} total_wait={total_wait:.1f}"
            )
            sleep_fn(wait_seconds)
            total_wait += wait_seconds
        attempts += 1
        try:
            return _sync_get_traffic_report(listing_ids, days, marketplace_id)
        except Exception as exc:  # noqa: BLE001 — discriminated below
            if not _is_rate_limited_error(exc):
                raise
            last_error_message = str(exc)
            if monotonic_fn() >= deadline:
                break
    raise TrafficReportRateLimitError(
        attempts=attempts,
        total_wait_seconds=total_wait,
        last_error=last_error_message,
    )


async def fetch_traffic_report_raw(
    listing_ids: list[str],
    days: int = 30,
    marketplace_id: str | None = None,
) -> dict[str, Any]:
    """REST Analytics traffic_report — RAW eBay JSON shape (Issue #31 Phase 2).

    Returns the raw eBay response (``header.metrics`` + ``records[*].dimensionValues``
    + ``records[*].metricValues``). Most callers should use ``fetch_traffic_report``
    instead, which applies ``parse_traffic_report_response`` and returns the
    decoded shape. This raw variant exists for tests that need to assert on
    eBay's wire format and for advanced callers that handle the positional
    decode themselves.

    Quota: each invocation accounts for one logical Sell Analytics call via
    call_accountant.account_call(api_namespace='sell_analytics'). Raises
    RateLimitError before contacting eBay if today's quota would be exceeded
    (#21 Phase 1).

    Burst-rate-limit (Issue #31 Phase 1): on HTTP 429 the call retries with
    backoff (5s, 15s, 60s; 80s wall-clock budget). After exhaustion raises
    ``TrafficReportRateLimitError`` so the caller can degrade gracefully or
    fail loud — the burst-retry logic stays out of every consumer.
    """
    # Quota gate first — fail loud BEFORE any network round-trip.
    from ebay.call_accountant import account_call  # noqa: PLC0415 — avoid import cycle

    account_call(api_namespace="sell_analytics")
    if marketplace_id is None:
        marketplace_id = str(_load_fees_config()["ebay_uk"]["marketplace_id"])
    return await asyncio.to_thread(
        _sync_get_traffic_report_with_retry, listing_ids, days, marketplace_id
    )


async def fetch_traffic_report(
    listing_ids: list[str],
    days: int = 30,
    marketplace_id: str | None = None,
) -> dict[str, Any]:
    """REST Analytics traffic_report — PARSED shape (Issue #31 Phase 2).

    Public surface for the orchestrator + MCP tools. Calls
    ``fetch_traffic_report_raw`` then applies
    ``parse_traffic_report_response`` so callers receive the decoded
    aggregate + per-listing breakdown directly.

    Returned shape (see ``parse_traffic_report_response`` for the full
    documentation):
        {
            "impressions": int, "views": int, "transactions": int,
            "ctr_pct": float | None, "sales_conversion_rate_pct": float | None,
            "search_impression_share_pct": float | None,
            "store_impression_share_pct": float | None,
            "search_view_share_pct": float | None,
            "organic_search_exposure_pct": float | None,
            "records_count": int,
            "per_listing": [{"listing_id", "metrics", "summary"}, ...],
            "per_listing_summary": {listing_id: {imp, views, tx_count, ...}},
            # plus abbreviated aliases (imp / tx_count / conv_pct) for the
            # ebay-ops Fetchers Protocol — see Phase 3 Gap 3.
        }

    Quota + burst-retry behaviour: identical to ``fetch_traffic_report_raw``
    (this wrapper does not issue an additional API call — parsing is local).
    """
    raw = await fetch_traffic_report_raw(listing_ids, days, marketplace_id)
    return parse_traffic_report_response(raw)


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
