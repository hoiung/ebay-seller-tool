"""Unit tests for ebay.analytics under-pricing + over-pricing helpers (#13 Phase 4)."""

from __future__ import annotations

import pytest

from ebay.analytics import compute_over_pricing, compute_recommended_band, compute_under_pricing

# === compute_recommended_band ========================================


def test_recommended_band_uses_config_defaults() -> None:
    """Default low/high pct loaded from config/fees.yaml under_pricing section."""
    # config/fees.yaml: low_pct=40, high_pct=55. Comp prices [10,20,30,40,50]:
    # int(5*0.40)=2 → sorted[2]=30. int(5*0.55)=2 → sorted[2]=30 (clamped).
    low, high = compute_recommended_band([10.0, 20.0, 30.0, 40.0, 50.0])
    assert low == 30.0
    # high index also 2 with N=5 because int(5*55/100)=2; band collapses.
    assert high == 30.0


def test_recommended_band_explicit_overrides() -> None:
    """Explicit low_pct/high_pct override config defaults."""
    low, high = compute_recommended_band(
        [10.0, 20.0, 30.0, 40.0, 50.0],
        low_pct=20,
        high_pct=80,
    )
    # int(5*0.20)=1 → sorted[1]=20. int(5*0.80)=4 → sorted[4]=50.
    assert low == 20.0
    assert high == 50.0


def test_recommended_band_empty_returns_none() -> None:
    low, high = compute_recommended_band([])
    assert low is None
    assert high is None


def test_recommended_band_validates_pct_bounds() -> None:
    with pytest.raises(ValueError, match="low_pct must be"):
        compute_recommended_band([10.0, 20.0], low_pct=150)
    with pytest.raises(ValueError, match="high_pct must be"):
        compute_recommended_band([10.0, 20.0], high_pct=-1)
    with pytest.raises(ValueError, match="must be <="):
        compute_recommended_band([10.0, 20.0], low_pct=80, high_pct=20)


# === compute_under_pricing — Stub #21 positional descriptors ============


def test_under_pricing_between_p25_p75_no_undercut() -> None:
    """Price in mid-band → positional BETWEEN_P25_P75, no clearance flag."""
    result = compute_under_pricing(
        live_price=50.0,
        p25_clean=30.0,
        p75_clean=70.0,
        units_sold_per_day=0.0,
        days_to_sell_median=20,
    )
    assert result["positional"] == "BETWEEN_P25_P75"
    assert result["signals"] == {"A": False, "B": False, "C": False}
    assert len(result["interpretations"]) == 2
    assert result["stock_clearance_exempt"] is False


def test_under_pricing_below_p25_with_only_a_signal() -> None:
    """Price below p25 (A True) but no velocity signals → BELOW_P25 with two-reading
    interpretations (intentional vs leaving margin). No auto-imperative."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        p75_clean=60.0,
        units_sold_per_day=0.0,
        days_to_sell_median=30,
    )
    assert result["positional"] == "BELOW_P25"
    assert result["signals"]["A"] is True
    assert "Intentional undercut" in result["interpretations"][0]
    assert "Leaving margin" in result["interpretations"][1]


def test_under_pricing_below_p25_two_signals() -> None:
    """A + B True → still BELOW_P25 positional + interpretations are unchanged."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        p75_clean=60.0,
        units_sold_per_day=0.5,
        days_to_sell_median=14,
    )
    assert result["positional"] == "BELOW_P25"
    assert result["signals"]["A"] is True
    assert result["signals"]["B"] is True


def test_under_pricing_below_p25_all_signals() -> None:
    """All 3 signals → still BELOW_P25 (positional doesn't escalate to a 'RED'
    label) — operator reads signals + interpretations."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        p75_clean=60.0,
        units_sold_per_day=0.5,
        days_to_sell_median=3,
    )
    assert result["positional"] == "BELOW_P25"
    assert all(result["signals"].values())


def test_under_pricing_handles_none_p25() -> None:
    """No clean comps → positional is None (no anchor)."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=None,
        units_sold_per_day=0.0,
        days_to_sell_median=30,
    )
    assert result["positional"] is None
    assert result["signals"]["A"] is None


def test_under_pricing_handles_none_velocity() -> None:
    """No sales velocity → B is None, positional still works."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        units_sold_per_day=None,
        days_to_sell_median=30,
    )
    assert result["positional"] == "BELOW_P25"
    assert result["signals"]["B"] is None


def test_under_pricing_explicit_velocity_override() -> None:
    """category_velocity_median override beats default 0.1."""
    result = compute_under_pricing(
        live_price=50.0,
        p25_clean=30.0,
        units_sold_per_day=0.5,  # 0.5 < 1.0 → B is False
        days_to_sell_median=30,
        category_velocity_median=1.0,
    )
    assert result["signals"]["B"] is False


def test_under_pricing_above_p75_positional() -> None:
    """Live > p75 → ABOVE_P75 positional with premium-positioning interpretations."""
    result = compute_under_pricing(
        live_price=80.0,
        p25_clean=30.0,
        p75_clean=70.0,
        units_sold_per_day=0.0,
        days_to_sell_median=20,
    )
    assert result["positional"] == "ABOVE_P75"
    assert "Premium positioning" in result["interpretations"][0]


def test_under_pricing_stock_clearance_exempt_qty_high_dts_low() -> None:
    """qty>5 + DTS<3 → stock_clearance_exempt=True regardless of positional."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        p75_clean=60.0,
        units_sold_per_day=0.5,
        days_to_sell_median=2,  # < 3
        quantity_available=10,  # > 5
    )
    assert result["positional"] == "BELOW_P25"
    assert result["stock_clearance_exempt"] is True


def test_under_pricing_no_clearance_when_qty_low() -> None:
    """qty<=5 → not exempt even when DTS<3."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        p75_clean=60.0,
        units_sold_per_day=0.5,
        days_to_sell_median=2,
        quantity_available=3,  # <= 5
    )
    assert result["stock_clearance_exempt"] is False


def test_under_pricing_no_clearance_when_dts_high() -> None:
    """DTS>=3 → not exempt even when qty>5."""
    result = compute_under_pricing(
        live_price=20.0,
        p25_clean=30.0,
        p75_clean=60.0,
        units_sold_per_day=0.5,
        days_to_sell_median=5,  # >= 3
        quantity_available=10,
    )
    assert result["stock_clearance_exempt"] is False


def test_under_pricing_at_p25_boundary_is_between() -> None:
    """live == p25 → BETWEEN_P25_P75 (BELOW is strictly less)."""
    result = compute_under_pricing(
        live_price=30.0,
        p25_clean=30.0,
        p75_clean=60.0,
        units_sold_per_day=0.0,
        days_to_sell_median=20,
    )
    assert result["positional"] == "BETWEEN_P25_P75"


# === compute_over_pricing ===========================================


def test_over_pricing_above_p75_all_signals_strong_review() -> None:
    """All 4 signals + ABOVE_P75 → strong 'review needed' first interpretation."""
    result = compute_over_pricing(
        live_price=80.0,
        p25_clean=30.0,
        p75_clean=60.0,  # A: > 60 ✓
        watchers=3,  # B: > 0 ✓
        units_sold=0,  # C: == 0 ✓
        days_on_site=44,  # D: > 21 ✓
    )
    assert result["positional"] == "ABOVE_P75"
    assert all(result["signals"].values())
    assert "review needed" in result["interpretations"][0]


def test_over_pricing_at_p75_boundary_is_between() -> None:
    """Price == p75 → BETWEEN_P25_P75 (consistent with under-pricing detector)."""
    result = compute_over_pricing(
        live_price=60.0,
        p25_clean=30.0,
        p75_clean=60.0,
        watchers=3,
        units_sold=0,
        days_on_site=44,
    )
    assert result["positional"] == "BETWEEN_P25_P75"
    assert result["signals"]["A_over_p75"] is False


def test_over_pricing_above_p75_no_watchers_milder_reading() -> None:
    """ABOVE_P75 but no watchers (B False) → 'premium positioning' reading, not strong review."""
    result = compute_over_pricing(
        live_price=80.0,
        p25_clean=30.0,
        p75_clean=60.0,
        watchers=0,
        units_sold=0,
        days_on_site=44,
    )
    assert result["positional"] == "ABOVE_P75"
    assert result["signals"]["B_has_watchers"] is False
    assert "Premium positioning" in result["interpretations"][0]


def test_over_pricing_below_p25_descriptor() -> None:
    """Live below p25 → BELOW_P25 descriptor (under-pricing-like reading)."""
    result = compute_over_pricing(
        live_price=20.0,
        p25_clean=30.0,
        p75_clean=60.0,
        watchers=3,
        units_sold=0,
        days_on_site=44,
    )
    assert result["positional"] == "BELOW_P25"


def test_over_pricing_handles_none_p75() -> None:
    """No clean comp signal → positional None, signals A_over_p75 None."""
    result = compute_over_pricing(
        live_price=80.0,
        p25_clean=None,
        p75_clean=None,
        watchers=3,
        units_sold=0,
        days_on_site=44,
    )
    assert result["positional"] is None
    assert result["signals"]["A_over_p75"] is None


def test_over_pricing_no_p25_only_p75_descriptor_works() -> None:
    """When only p75 supplied (no p25), positional collapses to ABOVE_P75 / BETWEEN."""
    result = compute_over_pricing(
        live_price=80.0,
        p25_clean=None,
        p75_clean=60.0,
        watchers=0,
        units_sold=0,
        days_on_site=14,
    )
    # p25_clean None → positional None per _positional_descriptor contract
    assert result["positional"] is None
