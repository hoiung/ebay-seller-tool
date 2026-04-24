"""Unit tests for ebay.analytics under-pricing + over-pricing helpers (#13 Phase 4)."""

from __future__ import annotations

from ebay.analytics import compute_over_pricing, compute_under_pricing


# === compute_under_pricing ===========================================


def test_under_pricing_ok_when_no_signals() -> None:
    """0/3 signals → verdict ok."""
    result = compute_under_pricing(
        live_price=50.0,
        p25_clean=30.0,
        units_sold_per_day=0.0,
        days_to_sell_median=20,
    )
    assert result["verdict"] == "ok"
    assert result["signals"] == {"A": False, "B": False, "C": False}
    assert result["recommended_floor"] is None
    assert result["recommended_ceiling"] is None


def test_under_pricing_ok_with_only_one_signal() -> None:
    """1/3 → ok (signal A only: price < p25)."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        units_sold_per_day=0.0,
        days_to_sell_median=30,
    )
    assert result["verdict"] == "ok"
    assert result["signals"]["A"] is True
    assert result["signals"]["B"] is False
    assert result["signals"]["C"] is False


def test_under_pricing_amber_with_two_signals() -> None:
    """2/3 → AMBER, recommended_floor = p40_clean."""
    result = compute_under_pricing(
        live_price=20.0,                    # A: < 30 ✓
        p25_clean=30.0,
        units_sold_per_day=0.5,             # B: > 0.1 ✓
        days_to_sell_median=14,             # C: not < 7 ✗
        p40_clean=35.0,
        p55_clean=42.0,
    )
    assert result["verdict"] == "AMBER"
    assert result["signals"]["A"] is True
    assert result["signals"]["B"] is True
    assert result["signals"]["C"] is False
    assert result["recommended_floor"] == 35.0
    assert result["recommended_ceiling"] is None  # AMBER doesn't set ceiling


def test_under_pricing_red_with_three_signals() -> None:
    """3/3 → RED, recommended_floor = p40_clean, recommended_ceiling = p55_clean."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        units_sold_per_day=0.5,
        days_to_sell_median=3,              # C: < 7 ✓
        p40_clean=35.0,
        p55_clean=42.0,
    )
    assert result["verdict"] == "RED"
    assert all(result["signals"].values())
    assert result["recommended_floor"] == 35.0
    assert result["recommended_ceiling"] == 42.0


def test_under_pricing_handles_none_p25() -> None:
    """No clean comps → A is None, count toward 0."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=None,
        units_sold_per_day=0.0,
        days_to_sell_median=30,
    )
    assert result["signals"]["A"] is None
    assert result["verdict"] == "ok"


def test_under_pricing_handles_none_velocity() -> None:
    """No sales velocity data → B is None."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        units_sold_per_day=None,
        days_to_sell_median=30,
    )
    assert result["signals"]["A"] is True
    assert result["signals"]["B"] is None  # undetermined


def test_under_pricing_explicit_velocity_override() -> None:
    """category_velocity_median override beats default 0.1."""
    result = compute_under_pricing(
        live_price=50.0,
        p25_clean=30.0,
        units_sold_per_day=0.5,             # 0.5 < 1.0 → B is False
        days_to_sell_median=30,
        category_velocity_median=1.0,
    )
    assert result["signals"]["B"] is False


def test_under_pricing_no_p40_no_floor_returned() -> None:
    """AMBER with p40_clean unset → recommended_floor stays None."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        units_sold_per_day=0.5,
        days_to_sell_median=3,
        # no p40_clean, no p55_clean
    )
    assert result["verdict"] == "RED"
    assert result["recommended_floor"] is None
    assert result["recommended_ceiling"] is None


# === compute_over_pricing ===========================================


def test_over_pricing_happy_path() -> None:
    """All 4 conditions → OVERPRICED."""
    result = compute_over_pricing(
        live_price=80.0,
        p75_clean=60.0,                     # A: > 60 ✓
        watchers=3,                         # B: > 0 ✓
        units_sold=0,                       # C: == 0 ✓
        days_on_site=44,                    # D: > 21 ✓
        p55_clean=50.0,
        p65_clean=58.0,
    )
    assert result["verdict"] == "OVERPRICED"
    assert all(result["signals"].values())
    assert result["recommended_floor"] == 50.0
    assert result["recommended_ceiling"] == 58.0


def test_over_pricing_ok_when_price_at_p75() -> None:
    """Price not over p75 → ok (A fails)."""
    result = compute_over_pricing(
        live_price=60.0,
        p75_clean=60.0,
        watchers=3,
        units_sold=0,
        days_on_site=44,
    )
    assert result["verdict"] == "ok"
    assert result["signals"]["A_over_p75"] is False


def test_over_pricing_ok_when_no_watchers() -> None:
    """Watchers=0 → ok (B fails — can't claim 'price is blocker' without interest)."""
    result = compute_over_pricing(
        live_price=80.0,
        p75_clean=60.0,
        watchers=0,
        units_sold=0,
        days_on_site=44,
    )
    assert result["verdict"] == "ok"
    assert result["signals"]["B_has_watchers"] is False


def test_over_pricing_ok_when_recent_sales() -> None:
    """units_sold > 0 → ok (C fails — selling, not stuck)."""
    result = compute_over_pricing(
        live_price=80.0,
        p75_clean=60.0,
        watchers=3,
        units_sold=2,
        days_on_site=44,
    )
    assert result["verdict"] == "ok"
    assert result["signals"]["C_no_sales"] is False


def test_over_pricing_ok_when_listing_too_fresh() -> None:
    """days_on_site <= 21 → ok (D fails — Cassini hasn't ranked yet)."""
    result = compute_over_pricing(
        live_price=80.0,
        p75_clean=60.0,
        watchers=3,
        units_sold=0,
        days_on_site=14,
    )
    assert result["verdict"] == "ok"
    assert result["signals"]["D_stale_21d"] is False


def test_over_pricing_handles_none_p75() -> None:
    """No clean comps → can't claim over-priced."""
    result = compute_over_pricing(
        live_price=80.0,
        p75_clean=None,
        watchers=3,
        units_sold=0,
        days_on_site=44,
    )
    assert result["verdict"] == "ok"
    assert result["signals"]["A_over_p75"] is None


def test_over_pricing_recommendations_only_when_triggered() -> None:
    """Verdict ok → no recommendations."""
    result = compute_over_pricing(
        live_price=50.0,
        p75_clean=60.0,
        watchers=3,
        units_sold=0,
        days_on_site=44,
        p55_clean=40.0,
        p65_clean=48.0,
    )
    assert result["verdict"] == "ok"
    assert result["recommended_floor"] is None
    assert result["recommended_ceiling"] is None
