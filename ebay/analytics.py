"""
Floor-price math + listing diagnostic synthesis.

Pure computation layer — no eBay API calls here. Trading-API extraction
lives in ebay/selling.py; REST Analytics + Post-Order in ebay/rest.py
(Phase 2). This module consumes their outputs.

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
            f"{p:.2%} and fvf {fvf_rate:.2%}: (1-p)(1-fvf)={((1-p)*(1-fvf_rate)):.4f} "
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
    view_count: int,
    watch_count: int,
    quantity_sold: int,
    question_count: int,
    days_on_site: int | None,
) -> dict[str, float | int | None]:
    """Derive Phase 1 funnel ratios from fields already in GetMyeBaySelling response.

    Phase 2 fills funnel.impressions + funnel.ctr_pct + signals.sales_conversion_rate_pct
    from REST Traffic Report (overriding conversion_rate_pct_approx here).
    """
    views_per_day: float | None = None
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
    watchers_per_100_views: float,
    sales_conversion_rate_pct: float | None,
) -> str:
    """STABLE | VOLATILE | INSUFFICIENT_DATA per research §2.4.

    Uses Phase 2 sales_conversion_rate_pct when available; falls back to
    Phase 1 watchers_per_100_views signal otherwise.
    """
    if days_on_site is None or days_on_site < 14:
        return "INSUFFICIENT_DATA"
    if sales_conversion_rate_pct is not None and sales_conversion_rate_pct >= 2.0:
        return "STABLE"
    if watchers_per_100_views >= 3.0:
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
    views = funnel.get("views") or 0
    watchers = funnel.get("watchers") or 0
    units_sold = funnel.get("units_sold") or 0
    watchers_per_100 = funnel.get("watchers_per_100_views") or 0.0
    conv_rate = funnel.get("conversion_rate_pct_approx") or 0.0

    if views < 20:
        return (
            "Low views — listing not being seen. Check title keywords, photos, "
            "and category — suggests Cassini exposure issue.",
            "Rewrite title with buyer search terms; refresh top photo.",
        )

    if views >= 50 and watchers == 0 and units_sold == 0:
        return (
            f"{views} views, 0 watchers, 0 sold — buyers see but don't engage. "
            "Photos, title, or price are not landing.",
            "Test a price drop 5-10%, then review photos if still no watchers.",
        )

    if watchers >= 5 and units_sold == 0:
        return (
            f"{watchers} watchers, 0 sold — price is the blocker (watchers = interested at higher price).",
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

    return (
        f"Middle-of-funnel: {views} views, {watchers} watchers, {units_sold} sold. "
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
    return (
        f"OK — £{delta:.2f} above floor £{floor:.2f} (return rate {return_rate:.1%}, {source})"
    )


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
