"""
Floor-price math + listing diagnostic synthesis.

Pure computation layer for the bulk of the module — Trading-API extraction
lives in ebay/selling.py; REST Analytics + Post-Order in ebay/rest.py
(Phase 2). This module consumes their outputs.

**Carve-out (Issue #14 Phase 3)**: ``_evaluate_wrong_direction_raise`` is
the single private async helper that breaks the pure-compute contract.
It calls ``fetch_competitor_prices`` and ``fetch_seller_transactions``
via *lazy imports* to keep the broader module free of API coupling at
import time. The lazy-import pattern is deliberate so unit tests can
patch the API surfaces without dragging the analytics layer into the
mock graph. Only ``update_listing`` may invoke the helper.

Formula (Issue #4, research §4):
    fixed  = cogs + per_order_fee + packaging + postage_out + time_sale
    return_extra = postage_return + time_return
    num    = fixed + p * return_extra + (1 - p) * fvf * postage_charged
    denom  = (1 - p) * (1 - fvf) - target_margin
    floor  = num / denom

Worked example (cogs=0, sunk time, 10% return rate, 15% margin, £0 postage_charged):
    fixed  = 0 + 0.40 + 0.60 + 3.50 + 0 = 4.50
    num    = 4.50 + 0.10 * 3.50 + 0.90 * 0.1548 * 0.0 = 4.85
    denom  = 0.90 * 0.8452 - 0.15 = 0.61068
    floor  = 4.85 / 0.61068 = 7.9418... -> £7.94
"""

from __future__ import annotations

import math
from typing import Any

from ebay.fees import _load_fees_config


def floor_price(
    cogs: float | None = None,
    return_rate: float | None = None,
    postage_out: float | None = None,
    postage_return: float | None = None,
    packaging: float | None = None,
    time_sale_gbp: float | None = None,
    time_return_gbp: float | None = None,
    fvf_rate: float | None = None,
    per_order_fee: float | None = None,
    target_margin: float | None = None,
    postage_charged: float = 0.0,
) -> dict[str, Any]:
    """Compute break-even floor price under a return-risk scenario.

    All `None`-default parameters read from config/fees.yaml at call-time.
    Pass a concrete value for any parameter to override.

    Returns dict with floor_gbp + suggested_ceiling_gbp + inputs echo.
    Raises ValueError if target_margin unreachable at given return rate.
    """
    cfg = _load_fees_config()

    if cogs is None:
        cogs = float(cfg["defaults"]["cogs_gbp"])
    if return_rate is None:
        return_rate = float(cfg["defaults"]["return_rate"])
    if postage_out is None:
        postage_out = float(cfg["postage"]["outbound_gbp"])
    if postage_return is None:
        postage_return = float(cfg["postage"]["return_gbp"])
    if packaging is None:
        packaging = float(cfg["packaging_gbp"])
    time_mode = cfg["time_cost"]["mode"]
    if time_sale_gbp is None:
        time_sale_gbp = float(cfg["time_cost"]["sale_gbp"])
    if time_return_gbp is None:
        time_return_gbp = float(cfg["time_cost"]["return_gbp"])
    if fvf_rate is None:
        fvf_rate = float(cfg["ebay_uk"]["fvf_rate"])
    if per_order_fee is None:
        per_order_fee = float(cfg["ebay_uk"]["per_order_fee_gbp"])
    if target_margin is None:
        target_margin = float(cfg["defaults"]["target_margin"])

    if not (0.0 <= return_rate < 1.0):
        raise ValueError(f"return_rate must be in [0, 1); got {return_rate}")
    if not (0.0 <= fvf_rate < 1.0):
        raise ValueError(f"fvf_rate must be in [0, 1); got {fvf_rate}")
    if target_margin >= 1.0:
        raise ValueError(f"target_margin must be < 1; got {target_margin}")

    p = return_rate
    fixed = cogs + per_order_fee + packaging + postage_out + time_sale_gbp
    return_extra = postage_return + time_return_gbp
    num = fixed + p * return_extra + (1 - p) * fvf_rate * postage_charged
    denom = (1 - p) * (1 - fvf_rate) - target_margin

    if denom <= 0:
        raise ValueError(
            f"target_margin {target_margin:.2%} unreachable at return_rate "
            f"{p:.2%} and fvf {fvf_rate:.2%}: (1-p)(1-fvf)={((1 - p) * (1 - fvf_rate)):.4f} "
            f"<= target_margin. Lower target_margin or accept higher risk."
        )

    floor = round(num / denom, 2)
    ceiling = round(floor * 1.5, 2)

    return {
        "floor_gbp": floor,
        "suggested_ceiling_gbp": ceiling,
        "inputs": {
            "cogs_gbp": cogs,
            "return_rate": p,
            "postage_out_gbp": postage_out,
            "postage_return_gbp": postage_return,
            "packaging_gbp": packaging,
            "time_sale_gbp": time_sale_gbp,
            "time_return_gbp": time_return_gbp,
            "time_cost_mode": time_mode,
            "fvf_rate": fvf_rate,
            "per_order_fee_gbp": per_order_fee,
            "target_margin": target_margin,
            "postage_charged_gbp": postage_charged,
        },
    }


def compute_funnel(
    view_count: int | None,
    watch_count: int,
    quantity_sold: int,
    question_count: int,
    days_on_site: int | None,
) -> dict[str, float | int | None]:
    """Derive Phase 1 funnel ratios from fields already in GetMyeBaySelling response.

    `view_count=None` means the Trading API HitCount field is absent / deprecated
    (the current eBay reality — see ebay/listings.py). In that case every
    view-dependent ratio is also None so the data-gap signal propagates to
    diagnose_listing / compute_rank_health.

    `view_count=0` is the GENUINE-ZERO case (eBay returned HitCount=0 on a
    legacy listing that still populates the field). Ratios stay at 0.0.

    Phase 2 fills funnel.impressions + funnel.ctr_pct + funnel.views and
    recomputes watchers_per_100_views / conversion_rate_pct_approx from the
    Analytics API LISTING_VIEWS_TOTAL; see server.py::analyse_listing.
    """
    if view_count is None:
        views_per_day: float | None = None
        watchers_per_100: float | None = None
        questions_per_100: float | None = None
        conversion_approx: float | None = None
    else:
        views_per_day = None
        if days_on_site and days_on_site > 0 and view_count > 0:
            views_per_day = round(view_count / days_on_site, 2)

        watchers_per_100 = 0.0
        if view_count > 0:
            watchers_per_100 = round(100.0 * watch_count / view_count, 2)

        questions_per_100 = 0.0
        if view_count > 0:
            questions_per_100 = round(100.0 * question_count / view_count, 2)

        conversion_approx = 0.0
        if view_count > 0:
            conversion_approx = round(100.0 * quantity_sold / view_count, 2)

    return {
        "impressions": None,
        "views": view_count,
        "watchers": watch_count,
        "units_sold": quantity_sold,
        "question_count": question_count,
        "views_per_day": views_per_day,
        "watchers_per_100_views": watchers_per_100,
        "questions_per_100_views": questions_per_100,
        "conversion_rate_pct_approx": conversion_approx,
        "ctr_pct": None,
    }


def compute_rank_health(
    days_on_site: int | None,
    watchers_per_100_views: float | None,
    sales_conversion_rate_pct: float | None,
    watchers: int = 0,
    units_sold: int = 0,
) -> str:
    """STABLE | VOLATILE | INSUFFICIENT_DATA per research §2.4.

    Uses Phase 2 sales_conversion_rate_pct when available; falls back to
    Phase 1 watchers_per_100_views signal otherwise. Absolute-signal
    fallback (watchers >= 5 AND units_sold > 0) covers the case where
    Phase 2 is unavailable / Traffic Report empty but the listing has a
    strong sales history — per SKILL.md: multi-qty sales history
    protects against price-revision rank resets.
    """
    if days_on_site is None or days_on_site < 14:
        return "INSUFFICIENT_DATA"
    if sales_conversion_rate_pct is not None and sales_conversion_rate_pct >= 2.0:
        return "STABLE"
    if watchers_per_100_views is not None and watchers_per_100_views >= 3.0:
        return "STABLE"
    if watchers >= 5 and units_sold > 0:
        return "STABLE"
    return "VOLATILE"


def diagnose_listing(
    funnel: dict[str, Any],
    signals: dict[str, Any],
    rank_health: str,
    price_gbp: float | None,
    floor_gbp: float,
) -> tuple[str, str | None]:
    """Map funnel + signals to (diagnosis_text, recommended_action).

    Decision matrix from research §2.3. Returns (text, action) — action is
    None when no change recommended.
    """
    views = funnel.get("views")  # preserves None — data-gap branch fires first below
    watchers = funnel.get("watchers") or 0
    units_sold = funnel.get("units_sold") or 0
    watchers_per_100 = funnel.get("watchers_per_100_views") or 0.0
    conv_rate = funnel.get("conversion_rate_pct_approx") or 0.0

    # Data-gap branch — fires when Phase 2 Traffic Report is unavailable
    # (OAuth not configured, Analytics API error) but Phase 1 signals
    # are positive. Prevents the old "Low views — rewrite title" false
    # alarm on listings with active watchers / prior sales.
    if views is None and (watchers > 0 or units_sold > 0):
        return (
            f"Data gap: Phase 2 Traffic Report unavailable. "
            f"Positive absolute signals present (watchers={watchers}, units_sold={units_sold}). "
            f"Configure OAuth (sell.analytics.readonly) to enable full funnel diagnosis.",
            None,
        )

    if views is not None and views < 20:
        return (
            "Low views — listing not being seen. Check title keywords, photos, "
            "and category — suggests Cassini exposure issue.",
            "Rewrite title with buyer search terms; refresh top photo.",
        )

    if views is not None and views >= 50 and watchers == 0 and units_sold == 0:
        return (
            f"{views} views, 0 watchers, 0 sold — buyers see but don't engage. "
            "Photos, title, or price are not landing.",
            "Test a price drop 5-10%, then review photos if still no watchers.",
        )

    if watchers >= 5 and units_sold == 0:
        return (
            f"{watchers} watchers, 0 sold — price is the blocker "
            "(watchers = interested at higher price).",
            "Drop price 5-8% or enable Best Offer.",
        )

    if units_sold > 0 and watchers_per_100 >= 3.0:
        return (
            f"Healthy listing — watchers-per-view ratio {watchers_per_100:.2f}/100 is strong; "
            f"conversion {conv_rate:.2f}% is mid-band for used HDDs. No fix required.",
            None,
        )

    if rank_health == "INSUFFICIENT_DATA":
        return (
            "Listing <14 days old — insufficient data for a verdict. Re-check after 14 days.",
            None,
        )

    views_str = views if views is not None else "N/A"
    return (
        f"Middle-of-funnel: {views_str} views, {watchers} watchers, {units_sold} sold. "
        "No clear single blocker — watch for 7 more days before acting.",
        None,
    )


def price_verdict(
    current_price: float | None,
    floor: float,
    return_rate: float,
    source: str,
) -> str:
    """Human-readable verdict string paired with floor_price output."""
    if current_price is None:
        return f"No price supplied — floor is £{floor:.2f} ({source})"
    delta = current_price - floor
    if delta < 0:
        return (
            f"BELOW FLOOR by £{abs(delta):.2f} — current £{current_price:.2f} "
            f"vs floor £{floor:.2f} (return rate {return_rate:.1%}, {source})"
        )
    return f"OK — £{delta:.2f} above floor £{floor:.2f} (return rate {return_rate:.1%}, {source})"


def summarise_feedback(feedback_entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate feedback comments + DSR into per-listing signals.

    Input: list of feedback dicts from get_listing_feedback.
    """
    if not feedback_entries:
        return {
            "feedback_positive_pct": None,
            "feedback_count": 0,
            "dsr_item_as_described": None,
        }
    positive = sum(1 for f in feedback_entries if f.get("comment_type") == "Positive")
    neg_or_neutral = sum(
        1 for f in feedback_entries if f.get("comment_type") in ("Negative", "Neutral")
    )
    total = positive + neg_or_neutral
    pct = round(100.0 * positive / total, 1) if total > 0 else None

    dsrs = [
        f.get("dsr_item_as_described")
        for f in feedback_entries
        if f.get("dsr_item_as_described") is not None
    ]
    dsr_avg = round(sum(dsrs) / len(dsrs), 2) if dsrs else None

    return {
        "feedback_positive_pct": pct,
        "feedback_count": total,
        "dsr_item_as_described": dsr_avg,
    }


def sell_through_rate(sold_count: int, unsold_count: int) -> float | None:
    """Percentage of SKUs that cleared in the window."""
    total = sold_count + unsold_count
    if total == 0:
        return None
    return round(100.0 * sold_count / total, 2)


# Issue #13 Phase 4 — under-pricing + over-pricing detectors.


def compute_recommended_band(
    comp_prices: list[float],
    low_pct: float | None = None,
    high_pct: float | None = None,
) -> tuple[float | None, float | None]:
    """Compute the (p_low, p_high) recommendation band from clean comp prices.

    Wires the `under_pricing.recommended_band_low_pct` / `_high_pct` keys
    in config/fees.yaml into runnable code. Skill orchestrator calls this
    helper between filter_clean_competitors and compute_under_pricing so
    the recommendation prices are config-driven, not hard-coded.

    Args:
        comp_prices: list of comp prices (post apple-to-apples filter +
            stale dropper). Empty list → (None, None).
        low_pct: percentile rank in [0, 100]. None → loaded from
            config/fees.yaml `under_pricing.recommended_band_low_pct`.
        high_pct: percentile rank in [0, 100]. None → loaded from
            config/fees.yaml `under_pricing.recommended_band_high_pct`.

    Returns:
        (p_low_price, p_high_price). Both None when comp_prices is empty.
    """
    if not comp_prices:
        return (None, None)
    if low_pct is None or high_pct is None:
        cfg = _load_fees_config()
        up_cfg = cfg.get("under_pricing", {})
        if low_pct is None:
            low_pct = float(up_cfg.get("recommended_band_low_pct", 40))
        if high_pct is None:
            high_pct = float(up_cfg.get("recommended_band_high_pct", 55))
    if not (0.0 <= low_pct <= 100.0):
        raise ValueError(f"low_pct must be in [0, 100]; got {low_pct}")
    if not (0.0 <= high_pct <= 100.0):
        raise ValueError(f"high_pct must be in [0, 100]; got {high_pct}")
    if low_pct > high_pct:
        raise ValueError(f"low_pct ({low_pct}) must be <= high_pct ({high_pct})")

    sorted_p = sorted(comp_prices)
    n = len(sorted_p)
    # Same percentile-rank arithmetic as ebay/browse.py (sorted[int(N*p/100)]
    # clamped to last index for the high band).
    low_idx = max(0, int(n * low_pct / 100.0))
    high_idx = min(n - 1, int(n * high_pct / 100.0))
    return (round(sorted_p[low_idx], 2), round(sorted_p[high_idx], 2))


def _positional_descriptor(
    live_price: float,
    p25_clean: float | None,
    p75_clean: float | None,
) -> str | None:
    """Stub #21 — bucket live_price relative to clean-comp percentiles.

    Returns one of "BELOW_P25" | "BETWEEN_P25_P75" | "ABOVE_P75". When p25_clean
    is None (no clean comp signal), returns None — the caller can interpret
    "no positional anchor available".

    Convention: BETWEEN_P25_P75 includes the p25/p75 values themselves
    (live_price == p25 → BETWEEN, not BELOW). BELOW is strictly less-than.
    """
    if p25_clean is None:
        return None
    if live_price < p25_clean:
        return "BELOW_P25"
    if p75_clean is None or live_price <= p75_clean:
        return "BETWEEN_P25_P75"
    return "ABOVE_P75"


def _stock_clearance_exempt(
    quantity_available: int | None,
    days_to_sell_median: int | None,
) -> bool:
    """Stub #21 stock-clearance exception: qty>5 + DTS<3 = goal, not defect.

    Pricing-review (13_ANALYTICS §2.3) treats fast-clearance multi-qty listings
    as INTENTIONAL undercut. Detectors flag the position but consumers should
    NOT treat it as a defect.
    """
    if quantity_available is None or days_to_sell_median is None:
        return False
    return quantity_available > 5 and days_to_sell_median < 3


def compute_under_pricing(
    live_price: float,
    p25_clean: float | None,
    units_sold_per_day: float | None,
    days_to_sell_median: int | None,
    category_velocity_median: float | None = None,
    *,
    p75_clean: float | None = None,
    quantity_available: int | None = None,
) -> dict[str, Any]:
    """Detect under-pricing — Stub #21 positional-descriptor refactor.

    Replaces the prior AMBER/RED/UNDERPRICED labels with neutral positional
    descriptors. The detector flags a price's POSITION in the clean comp
    distribution; the consumer (operator / pricing-review skill) decides
    whether the position is intentional or unintentional based on context.

    Signals (each True/False, None = undetermined):
      A. live_price < p25_clean (cheaper than 75% of clean apple-to-apples)
      B. units_sold_per_day > category_velocity_median (selling fast)
      C. days_to_sell_median < 7 (recent sales clear within a week)

    Returns:
        {
            "positional": "BELOW_P25" | "BETWEEN_P25_P75" | "ABOVE_P75" | None,
            "signals": {"A": bool|None, "B": bool|None, "C": bool|None},
            "interpretations": [str, str],  # two readings, no auto-imperative
            "stock_clearance_exempt": bool,  # qty>5 + DTS<3 = goal, not defect
        }

    Args:
        live_price: current eBay listing price (GBP).
        p25_clean: 25th percentile of clean comp prices. None → positional=None.
        units_sold_per_day: per-SKU sales velocity.
        days_to_sell_median: median time-to-clear for sold units.
        category_velocity_median: category-baseline velocity. None loads from
            config/fees.yaml `under_pricing.velocity_median_default`.
        p75_clean: 75th percentile for positional anchoring (BETWEEN vs ABOVE).
        quantity_available: stock count for stock-clearance exemption.
    """
    if category_velocity_median is None:
        cfg = _load_fees_config()
        category_velocity_median = float(
            cfg.get("under_pricing", {}).get("velocity_median_default", 0.1)
        )

    a = (p25_clean is not None) and (live_price < p25_clean)
    b = (units_sold_per_day is not None) and (units_sold_per_day > category_velocity_median)
    c = (days_to_sell_median is not None) and (days_to_sell_median < 7)

    positional = _positional_descriptor(live_price, p25_clean, p75_clean)
    exempt = _stock_clearance_exempt(quantity_available, days_to_sell_median)

    interpretations: list[str] = []
    if positional == "BELOW_P25":
        interpretations = [
            "Intentional undercut to clear stock (DTS<3 + multi-qty supports)",
            "Leaving margin on table (no clearance posture, single-qty)",
        ]
    elif positional == "BETWEEN_P25_P75":
        interpretations = [
            "Mid-pack pricing — typical for steady-velocity SKUs",
            "Could push toward p75 if conversion is strong (watch CTR + watch_count)",
        ]
    elif positional == "ABOVE_P75":
        interpretations = [
            "Premium positioning — works when listing has clear differentiator "
            "(caddy / warranty / Top-Rated)",
            "May suppress conversion if no differentiator visible — "
            "consider Best Offer or moderate drop",
        ]

    return {
        "positional": positional,
        "signals": {
            "A": a if p25_clean is not None else None,
            "B": b if units_sold_per_day is not None else None,
            "C": c if days_to_sell_median is not None else None,
        },
        "interpretations": interpretations,
        "stock_clearance_exempt": exempt,
    }


def compute_over_pricing(
    live_price: float,
    p75_clean: float | None,
    watchers: int,
    units_sold: int,
    days_on_site: int | None,
    *,
    p25_clean: float | None = None,
    quantity_available: int | None = None,
) -> dict[str, Any]:
    """Detect over-pricing — Stub #21 positional-descriptor refactor.

    Same `positional` + `interpretations` + `stock_clearance_exempt` envelope
    as `compute_under_pricing`. The `signals` dict has DIFFERENT keys here
    because over-pricing is observed via different evidence (watchers /
    sales / staleness rather than velocity-vs-category):

        compute_under_pricing.signals: {"A", "B", "C"}
        compute_over_pricing.signals:  {"A_over_p75", "B_has_watchers",
                                         "C_no_sales", "D_stale_21d"}

    Consumers should access signal keys by their over/under-specific name —
    do NOT assume the dicts are interchangeable.

    Signals:
      A_over_p75   — live_price > p75_clean
      B_has_watchers — watchers > 0
      C_no_sales   — units_sold == 0
      D_stale_21d  — days_on_site > 21

    When all 4 fire, the ABOVE_P75 positional is paired with the strongest
    "needs review" interpretation. When only A fires (price above p75 but
    converting), the BELOW_P25-style "premium positioning" reading dominates.

    `stock_clearance_exempt` is always `False` for over-pricing — the
    exemption only fires on the under-pricing BELOW_P25 path (a high-priced
    multi-qty listing is not a clearance scenario by definition). The key
    is included so the response shape mirrors `compute_under_pricing` for
    consumers that read both.

    Returns:
        {
            "positional": "BELOW_P25" | "BETWEEN_P25_P75" | "ABOVE_P75" | None,
            "signals": {"A_over_p75", "B_has_watchers", "C_no_sales", "D_stale_21d"},
            "interpretations": [str, str],
            "stock_clearance_exempt": False,
        }
    """
    a = (p75_clean is not None) and (live_price > p75_clean)
    b = watchers > 0
    c = units_sold == 0
    d = (days_on_site is not None) and (days_on_site > 21)

    positional = _positional_descriptor(live_price, p25_clean, p75_clean)
    # Sonnet Ralph LOW — remove the always-False `_stock_clearance_exempt(qty, None)`
    # call. Over-pricing has no clearance scenario; document the False explicitly.
    # `quantity_available` is retained as a parameter for caller-shape symmetry.
    _ = quantity_available  # documented unused — see docstring
    exempt = False

    interpretations: list[str] = []
    if positional == "BELOW_P25":
        interpretations = [
            "Intentional undercut to clear stock",
            "Leaving margin on table",
        ]
    elif positional == "BETWEEN_P25_P75":
        interpretations = [
            "Mid-pack pricing — typical for steady-velocity SKUs",
            "Could push toward p75 if conversion is strong",
        ]
    elif positional == "ABOVE_P75":
        if a and b and c and d:
            interpretations = [
                "Above-market price + interest but no conversion + stale — review needed "
                "(consider Best Offer or drop to p55-p65)",
                "Niche premium that hasn't found its buyer yet (rare; only if "
                "differentiator clearly visible in listing)",
            ]
        else:
            interpretations = [
                "Premium positioning — works when listing has clear differentiator",
                "May suppress conversion if no differentiator visible — "
                "watch watchers + days_on_site",
            ]

    return {
        "positional": positional,
        "signals": {
            "A_over_p75": a if p75_clean is not None else None,
            "B_has_watchers": b,
            "C_no_sales": c,
            "D_stale_21d": d if days_on_site is not None else None,
        },
        "interpretations": interpretations,
        "stock_clearance_exempt": exempt,
    }


async def _evaluate_wrong_direction_raise(
    item_id: str,
    old_price: float,
    new_price: float,
    item_full: dict[str, Any],
    sales_window_days: int = 14,
    min_units_sold: int = 1,
) -> dict[str, Any] | None:
    """Issue #14 Phase 3 — fire WARN when a price RAISE looks wrong-direction.

    Returns a warning dict on trigger, or None when the raise looks fine.

    Caller passes update_listing's existing ``current_full = listing_to_dict(
    current.reply.Item)`` (server.py:650) as ``item_full`` — NOT the
    ``snapshot_listing`` ``before`` dict. ``snapshot_listing`` returns only
    basic fields (no specifics, no watch_count); this helper accesses MPN,
    condition_name, watch_count, quantity_available which require the
    listing_to_dict shape. NO new GetItem call inside the helper —
    caller-side reuse only.

    Triggers WARN when ALL hold:
      1. ``new_price > old_price`` (already checked at call site).
      2. ``units_sold`` for ``item_id`` in last ``sales_window_days`` days
         is at least ``min_units_sold`` (from GetSellerTransactions).
      3. ``positional ≠ BELOW_P25`` (raising INTO market mid-band is fine).
      4. NOT ``_stock_clearance_exempt`` (Stub #21: qty>5 + DTS<3 is
         intentional clearance, not defect — known scope_gap for qty=2-5).
      5. ``comp_verdict ∉ {LONE_SUPPLIER, THIN_POOL, ALL_FILTERED}`` (no /
         weak market signal to validate against — Stub #20).
      6. ``concentration.confidence != 'low'`` (Stub #19 — comp pool
         dominated by single seller is not reliable signal).

    Restock context is operator-mental in v1: WARN fires on velocity +
    positional + comp signal alone; the recommendation string surfaces
    restock framing for operator judgement. No `restock` config knob.

    Args:
        item_id: eBay item ID for the listing being raised.
        old_price: pre-raise live price (GBP).
        new_price: proposed new price (GBP).
        item_full: ``listing_to_dict()`` shape (specifics, condition_name,
            watch_count, quantity_available).
        sales_window_days: window for "did this listing recently sell?"
            (config: ``wrong_direction_warn.sales_window_days``).
        min_units_sold: minimum units in window to consider velocity > 0
            (config: ``wrong_direction_warn.min_units_sold``).

    Returns:
        ``None`` when the raise is fine OR signals are missing OR the comp
        pool is too thin to judge. ``dict`` with rule + delta + units_sold
        + recommendation when the raise looks wrong-direction.
    """
    # Lazy imports — analytics.py is otherwise a pure compute layer; the
    # async fetchers it touches here are operationally bounded to this one
    # helper. Importing at module top would couple the whole module to the
    # eBay API surface.
    from ebay.browse import fetch_competitor_prices  # noqa: PLC0415
    from ebay.client import log_warn  # noqa: PLC0415
    from ebay.selling import fetch_seller_transactions  # noqa: PLC0415

    # 1. Sales-velocity check.
    try:
        txns = await fetch_seller_transactions(days=sales_window_days)
    except Exception as e:  # noqa: BLE001
        # Velocity unknown — be conservative, don't fire spurious WARN.
        # Per AP #12: surface the silent skip so operator sees WHY the WARN
        # didn't fire (transient API failure vs no recent sales).
        log_warn(
            f"wrong_direction_eval skipped item={item_id} "
            f"stage=fetch_seller_transactions reason={type(e).__name__}: {e}"
        )
        return None
    units_sold = 0
    for t in txns.get("transactions", []) or []:
        if str(t.get("item_id")) == str(item_id):
            try:
                units_sold += int(t.get("quantity_purchased") or 0)
            except (TypeError, ValueError):
                continue
    if units_sold < min_units_sold:
        return None  # No recent sales → raise is sensible (stalled listing).

    # 2. Comp-pool + positional check. Need MPN + condition_name from the
    # listing_to_dict shape. Missing either short-circuits to None (can't
    # judge without a market signal).
    specifics = item_full.get("specifics") or {}
    mpn_list = specifics.get("MPN") if isinstance(specifics, dict) else None
    if not mpn_list or not isinstance(mpn_list, list) or not mpn_list[0]:
        return None
    mpn = str(mpn_list[0]).strip()
    condition_name = item_full.get("condition_name") or "USED"
    # eBay condition values vary in case; fetch_competitor_prices accepts
    # NEW / USED / USED_EXCELLENT / OPENED / FOR_PARTS literals.
    # secret-allow: false positive on "_token" variable name — this is an
    # eBay condition enum literal (NEW / USED / OPENED / FOR_PARTS), not a
    # credential.
    cond_value = "USED" if str(condition_name).lower().startswith("used") else str(
        condition_name
    ).upper().replace(" ", "_")

    try:
        comp_result = await fetch_competitor_prices(
            part_number=mpn,
            condition=cond_value,
            own_listing=item_full,
            own_live_price=old_price,
        )
    except Exception as e:  # noqa: BLE001
        log_warn(
            f"wrong_direction_eval skipped item={item_id} "
            f"stage=fetch_competitor_prices reason={type(e).__name__}: {e}"
        )
        return None
    verdict = comp_result.get("verdict")
    if verdict in {"LONE_SUPPLIER", "THIN_POOL", "ALL_FILTERED"}:
        # Stub #20 — no usable market signal to anchor against.
        return None
    audit = comp_result.get("audit") or {}
    concentration = audit.get("concentration") or {}
    if concentration.get("confidence") == "low":
        # Stub #19 — comp pool dominated by single seller; signal unreliable.
        return None

    pricing = compute_under_pricing(
        live_price=old_price,
        p25_clean=comp_result.get("p25"),
        p75_clean=comp_result.get("p75"),
        units_sold_per_day=units_sold / max(sales_window_days, 1),
        days_to_sell_median=item_full.get("days_to_sell_median"),
        quantity_available=item_full.get("quantity_available"),
    )
    if pricing.get("positional") == "BELOW_P25":
        # Raising INTO market mid-band is sensible.
        return None
    if pricing.get("stock_clearance_exempt") is True:
        # Multi-qty fast-clearance — intentional posture, not defect.
        return None

    # All gates passed → fire WARN.
    watch_count = int(item_full.get("watch_count") or 0)
    delta_pct = (new_price - old_price) / old_price * 100.0
    return {
        "rule": "wrong_direction_raise_v1",
        "old_price": old_price,
        "new_price": new_price,
        "delta_pct": round(delta_pct, 2),
        "units_sold_window_days": sales_window_days,
        "units_sold": units_sold,
        "watch_count": watch_count,
        "comp_positional_pre_raise": pricing.get("positional"),
        "recommendation": (
            f"Item sold {units_sold} unit(s) in last {sales_window_days}d at £{old_price:.2f}. "
            f"Position pre-raise: {pricing.get('positional')}. "
            f"Raising risks killing velocity. Confirm restock-context "
            f"(raise OK if restock=YES; hold/drop if restock=NO)."
        ),
    }


def _round_down_to_pound(pct: float, live_price: float) -> int:
    """Apply pct to live_price, floor to nearest whole pound (psychological discount)."""
    return math.floor(pct * live_price)


def _validate_best_offer_config(cfg_bo: dict[str, Any]) -> None:
    """Fail Fast on malformed best_offer config — typo'd key names silently
    fall back to defaults otherwise (violates Engineering Requirements
    Fail Fast). Caller catches ValueError + surfaces via log_warn.

    Invariants:
      - All 4 keys present (auto_accept_pct, auto_decline_pct, counter_offer_pct,
        round_down_to_pound)
      - 3 pct values in [0.0, 1.0]
      - round_down_to_pound is bool
      - auto_decline_pct < auto_accept_pct < 1.0
      - counter_offer_pct >= auto_decline_pct
    """
    if not cfg_bo:
        return  # absent block — caller handles via fallback path
    required = {"auto_accept_pct", "auto_decline_pct", "counter_offer_pct", "round_down_to_pound"}
    missing = required - set(cfg_bo.keys())
    if missing:
        raise ValueError(
            f"config/fees.yaml best_offer block invalid: missing keys {sorted(missing)} — "
            f"check for typos (e.g. auto_acceptpct vs auto_accept_pct)"
        )
    pct_keys = ("auto_accept_pct", "auto_decline_pct", "counter_offer_pct")
    for key in pct_keys:
        val = cfg_bo[key]
        if not isinstance(val, (int, float)) or not (0.0 <= float(val) <= 1.0):
            raise ValueError(
                f"config/fees.yaml best_offer.{key}={val!r} invalid — must be float in [0.0, 1.0]"
            )
    if not isinstance(cfg_bo["round_down_to_pound"], bool):
        raise ValueError(
            f"config/fees.yaml best_offer.round_down_to_pound={cfg_bo['round_down_to_pound']!r} "
            f"invalid — must be bool"
        )
    a, d, c = cfg_bo["auto_accept_pct"], cfg_bo["auto_decline_pct"], cfg_bo["counter_offer_pct"]
    if not (d < a < 1.0):
        raise ValueError(
            f"config/fees.yaml best_offer invariant violated: "
            f"auto_decline_pct ({d}) < auto_accept_pct ({a}) < 1.0 must hold"
        )
    if c < d:
        raise ValueError(
            f"config/fees.yaml best_offer invariant violated: "
            f"counter_offer_pct ({c}) >= auto_decline_pct ({d}) must hold"
        )


def compute_best_offer_thresholds(
    floor_gbp: float,
    live_price_gbp: float,
    auto_accept_pct: float | None = None,
    auto_decline_pct: float | None = None,
    floor_buffer_pct: float = 0.05,
) -> dict[str, Any]:
    """G-NEW-1 (Issue #4) + Issue #16 — recommend Best Offer thresholds.

    Pure-function helper. Two consumer modes:

    1) **Diagnostic** (server.py recommend_best_offer_thresholds, analyse_listing
       best_offer suggestion, enable_best_offer_all.py Phase 6 dry-run): caller
       supplies floor + live price. When pcts are None (default), reads
       config/fees.yaml `best_offer:` block; when explicit, kwargs win. This is
       the path Issue #16 makes config-driven so analyse_listing and the storewide
       enable script align with the operator-locked 0.925/0.75 thresholds.

    2) **Backward-compat fallback**: when neither config nor kwargs supply pcts
       (fresh install with no `best_offer:` block), falls back to the historical
       0.88/0.72 pair so pre-#16 callers don't crash. Schema validator (Fail
       Fast) raises ValueError on a partially-populated/typo'd block — silent
       fallback only on TOTALLY ABSENT block.

    Composition rule:
        auto_accept = max(floor * (1 + buffer), pct * live_price)
        auto_decline = max(floor, decline_pct * live_price)
        when round_down_to_pound: apply math.floor() to both

    The 5% buffer above floor defends margin: floor is break-even; auto-accepting
    AT floor accepts every offer that just clears break-even. Buffer ensures a
    minimum profit slice on auto-accepts.

    NOTE (Issue #16 AC3.2): The autonomous responder
    (`scripts/respond_best_offers.py`) does NOT call this function — it reads
    config directly and applies a pure formula without floor_buffer_pct. Reason:
    the responder is the contract AUTHOR for offers (it WRITES the threshold),
    not a diagnostic SUGGESTOR. Buffer-pct over-recommends accept thresholds
    relative to the operator-locked 92.5% rule on low-priced listings.
    """
    if floor_buffer_pct < 0:
        raise ValueError(
            f"floor_buffer_pct={floor_buffer_pct} invalid — must be >= 0 (programming error)"
        )

    cfg_bo = _load_fees_config().get("best_offer", {}) or {}
    _validate_best_offer_config(cfg_bo)  # Fail Fast on partial/typo'd block

    if auto_accept_pct is None:
        auto_accept_pct = cfg_bo.get("auto_accept_pct", 0.88)
    if auto_decline_pct is None:
        auto_decline_pct = cfg_bo.get("auto_decline_pct", 0.72)
    round_down = bool(cfg_bo.get("round_down_to_pound", False))

    floor_with_buffer = floor_gbp * (1.0 + floor_buffer_pct)
    auto_accept_raw = auto_accept_pct * live_price_gbp
    auto_decline_raw = auto_decline_pct * live_price_gbp

    auto_accept = max(floor_with_buffer, auto_accept_raw)
    auto_decline = max(floor_gbp, auto_decline_raw)

    if round_down:
        auto_accept = math.floor(auto_accept)
        auto_decline = math.floor(auto_decline)

    rationale = (
        f"auto_accept = max(floor*{1.0 + floor_buffer_pct:.2f}={floor_with_buffer:.2f}, "
        f"{auto_accept_pct:.0%}*live={auto_accept_raw:.2f}); "
        f"auto_decline = max(floor={floor_gbp:.2f}, "
        f"{auto_decline_pct:.0%}*live={auto_decline_raw:.2f})"
        + (" [round_down_to_pound applied]" if round_down else "")
    )

    return {
        "auto_accept_gbp": auto_accept if round_down else round(auto_accept, 2),
        "auto_decline_gbp": auto_decline if round_down else round(auto_decline, 2),
        "floor_gbp": round(floor_gbp, 2),
        "rationale": rationale,
    }
