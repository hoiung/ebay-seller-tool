"""Unit tests for ebay.analytics.floor_price math (Issue #4 AC 1.8)."""

import pytest

from ebay.analytics import (
    compute_funnel,
    compute_rank_health,
    diagnose_listing,
    floor_price,
    price_verdict,
    sell_through_rate,
    summarise_feedback,
)
from ebay.fees import reset_fees_cache


def setup_function() -> None:
    reset_fees_cache()


def test_floor_price_zero_cogs_10_return_sunk_time() -> None:
    """Worked example from issue body: £7.94 under defaults."""
    result = floor_price(cogs=0.0, return_rate=0.10)
    assert result["floor_gbp"] == pytest.approx(7.94, abs=0.01)
    assert result["suggested_ceiling_gbp"] == pytest.approx(11.91, abs=0.01)
    assert result["inputs"]["time_cost_mode"] == "sunk"


def test_floor_price_zero_return_zero_cogs() -> None:
    """No return risk, no COGS — minimum possible floor."""
    result = floor_price(cogs=0.0, return_rate=0.0)
    # fixed = 0 + 0.40 + 0.60 + 3.50 + 0 = 4.50
    # denom = 1 * (1 - 0.1548) - 0.15 = 0.6952
    # floor = 4.50 / 0.6952 = 6.47
    assert result["floor_gbp"] == pytest.approx(6.47, abs=0.02)


def test_floor_price_15_return_zero_cogs() -> None:
    """At 15% return rate the £35 price has headroom."""
    result = floor_price(cogs=0.0, return_rate=0.15)
    # fixed = 4.50
    # num = 4.50 + 0.15*3.50 = 5.025
    # denom = 0.85*0.8452 - 0.15 = 0.56842
    # floor = 5.025 / 0.56842 = 8.84
    assert result["floor_gbp"] == pytest.approx(8.84, abs=0.02)


def test_floor_price_user_override_cogs() -> None:
    """Paid-for stock override — floor rises."""
    result = floor_price(cogs=15.0, return_rate=0.10)
    assert result["floor_gbp"] > 30.0  # paid £15 + return risk pushes floor well up


def test_floor_price_unreachable_margin_raises() -> None:
    """85% target margin exceeds (1-p)(1-fvf) = 0.76 → denom <= 0 → unreachable."""
    with pytest.raises(ValueError, match="unreachable"):
        floor_price(cogs=0.0, return_rate=0.10, target_margin=0.85)


def test_floor_price_bad_return_rate_raises() -> None:
    with pytest.raises(ValueError, match="return_rate"):
        floor_price(return_rate=1.5)


def test_floor_price_postage_charged_extra_fvf() -> None:
    """Per spec formula: non-zero postage_charged adds fvf cost → floor rises."""
    r0 = floor_price(cogs=0.0, return_rate=0.10, postage_charged=0.0)
    r5 = floor_price(cogs=0.0, return_rate=0.10, postage_charged=5.0)
    # num delta = (1-p)*fvf*postage_charged = 0.9 * 0.1548 * 5.0 = 0.6966
    # denom unchanged → floor rises by delta/denom
    assert r5["floor_gbp"] > r0["floor_gbp"]


def test_compute_funnel_basic() -> None:
    f = compute_funnel(view_count=100, watch_count=5, quantity_sold=2, question_count=1, days_on_site=10)
    assert f["views"] == 100
    assert f["watchers"] == 5
    assert f["watchers_per_100_views"] == 5.0
    assert f["conversion_rate_pct_approx"] == 2.0
    assert f["views_per_day"] == 10.0
    assert f["impressions"] is None  # Phase 2 fills
    assert f["ctr_pct"] is None


def test_compute_funnel_zero_views() -> None:
    # Genuine-zero path: view_count=0 (not None) → ratios stay 0.0
    f = compute_funnel(view_count=0, watch_count=0, quantity_sold=0, question_count=0, days_on_site=None)
    assert f["watchers_per_100_views"] == 0.0
    assert f["conversion_rate_pct_approx"] == 0.0
    assert f["views_per_day"] is None


def test_compute_funnel_views_none() -> None:
    """AC-5.1: view_count=None → every view-dependent ratio is None, not 0.0.

    Preserves the data-gap signal so diagnose_listing can fire its data-gap
    branch instead of the false-alarm 'Low views — rewrite title' branch.
    """
    f = compute_funnel(view_count=None, watch_count=7, quantity_sold=5, question_count=0, days_on_site=20)
    assert f["views"] is None
    assert f["watchers_per_100_views"] is None
    assert f["conversion_rate_pct_approx"] is None
    assert f["views_per_day"] is None
    assert f["questions_per_100_views"] is None
    # Non-view fields still populated from raw counts
    assert f["watchers"] == 7
    assert f["units_sold"] == 5


def test_compute_rank_health_insufficient_data() -> None:
    assert compute_rank_health(days_on_site=5, watchers_per_100_views=10.0, sales_conversion_rate_pct=None) == "INSUFFICIENT_DATA"


def test_compute_rank_health_stable_by_watchers() -> None:
    assert compute_rank_health(days_on_site=20, watchers_per_100_views=5.0, sales_conversion_rate_pct=None) == "STABLE"


def test_compute_rank_health_stable_by_conversion() -> None:
    assert compute_rank_health(days_on_site=20, watchers_per_100_views=1.0, sales_conversion_rate_pct=3.0) == "STABLE"


def test_compute_rank_health_stable_by_absolute_signals() -> None:
    """AC-5.2: Phase-2-unavailable fallback — watchers >= 5 AND units_sold > 0 → STABLE.

    Matches the live 287260458724 scenario before the decision-tree fix: both
    ratio signals are None (Phase 2 unavailable), but 7 watchers + 5 sold over
    20 days is clearly a healthy listing.
    """
    assert (
        compute_rank_health(
            days_on_site=20,
            watchers_per_100_views=None,
            sales_conversion_rate_pct=None,
            watchers=7,
            units_sold=5,
        )
        == "STABLE"
    )


def test_compute_rank_health_volatile() -> None:
    assert compute_rank_health(days_on_site=20, watchers_per_100_views=1.0, sales_conversion_rate_pct=0.5) == "VOLATILE"


def test_compute_rank_health_volatile_when_absolute_signals_too_weak() -> None:
    """Absolute-signal fallback requires BOTH watchers>=5 AND units_sold>0."""
    # 4 watchers, 10 sold — below watchers threshold
    assert (
        compute_rank_health(
            days_on_site=20,
            watchers_per_100_views=None,
            sales_conversion_rate_pct=None,
            watchers=4,
            units_sold=10,
        )
        == "VOLATILE"
    )
    # 10 watchers, 0 sold — below units_sold threshold
    assert (
        compute_rank_health(
            days_on_site=20,
            watchers_per_100_views=None,
            sales_conversion_rate_pct=None,
            watchers=10,
            units_sold=0,
        )
        == "VOLATILE"
    )


def test_diagnose_low_views() -> None:
    funnel = compute_funnel(view_count=5, watch_count=0, quantity_sold=0, question_count=0, days_on_site=20)
    diag, action = diagnose_listing(funnel, {}, "VOLATILE", price_gbp=35.0, floor_gbp=7.94)
    assert "Low views" in diag
    assert action is not None


def test_diagnose_data_gap_aware() -> None:
    """AC-5.3: views=None + positive absolute signals → data-gap diagnosis, NOT 'Low views'.

    Regression guard for the 287260458724 failure: pre-fix engine returned
    'Low views — rewrite title' on a listing with 7 watchers + 5 sold. Post-fix
    the data-gap branch fires instead.
    """
    funnel = compute_funnel(
        view_count=None, watch_count=7, quantity_sold=5, question_count=0, days_on_site=20
    )
    diag, action = diagnose_listing(funnel, {}, "STABLE", price_gbp=35.0, floor_gbp=7.94)
    assert "Data gap" in diag
    assert "Low views" not in diag
    assert "Rewrite title" not in diag
    assert "watchers=7" in diag
    assert "units_sold=5" in diag
    assert action is None


def test_diagnose_watchers_no_sale() -> None:
    funnel = compute_funnel(view_count=100, watch_count=10, quantity_sold=0, question_count=0, days_on_site=20)
    diag, action = diagnose_listing(funnel, {}, "VOLATILE", price_gbp=35.0, floor_gbp=7.94)
    assert "price is the blocker" in diag
    assert "Drop price" in action or "Best Offer" in action


def test_diagnose_healthy() -> None:
    funnel = compute_funnel(view_count=100, watch_count=5, quantity_sold=2, question_count=0, days_on_site=20)
    diag, action = diagnose_listing(funnel, {}, "STABLE", price_gbp=35.0, floor_gbp=7.94)
    assert "Healthy" in diag
    assert action is None


def test_price_verdict_above_floor() -> None:
    v = price_verdict(current_price=35.0, floor=7.94, return_rate=0.10, source="defaults")
    assert "OK" in v and "above floor" in v


def test_price_verdict_below_floor() -> None:
    v = price_verdict(current_price=5.0, floor=7.94, return_rate=0.10, source="defaults")
    assert "BELOW FLOOR" in v


def test_sell_through_rate() -> None:
    assert sell_through_rate(3, 7) == 30.0
    assert sell_through_rate(0, 0) is None


def test_summarise_feedback_positive_count() -> None:
    entries = [
        {"comment_type": "Positive", "dsr_item_as_described": 5.0},
        {"comment_type": "Positive", "dsr_item_as_described": 4.0},
        {"comment_type": "Negative", "dsr_item_as_described": 2.0},
    ]
    s = summarise_feedback(entries)
    assert s["feedback_count"] == 3
    assert s["feedback_positive_pct"] == pytest.approx(66.7, abs=0.1)
    assert s["dsr_item_as_described"] == pytest.approx(3.67, abs=0.01)


def test_summarise_feedback_empty() -> None:
    s = summarise_feedback([])
    assert s["feedback_count"] == 0
    assert s["feedback_positive_pct"] is None
    assert s["dsr_item_as_described"] is None
