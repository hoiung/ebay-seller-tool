"""Pure-function tests for ebay.analytics.compute_best_offer_thresholds (G-NEW-1)."""

from __future__ import annotations

from ebay.analytics import compute_best_offer_thresholds


def test_canonical_88_72_against_50_quid_listing() -> None:
    """Floor £18, live £50 → auto_accept = max(18.90, 44.00) = 44.00; auto_decline = max(18, 36) = 36."""
    r = compute_best_offer_thresholds(floor_gbp=18.0, live_price_gbp=50.0)
    assert r["auto_accept_gbp"] == 44.0
    assert r["auto_decline_gbp"] == 36.0
    assert r["floor_gbp"] == 18.0
    assert "auto_accept = max(" in r["rationale"]


def test_floor_buffer_dominates_when_live_low() -> None:
    """Live £20, floor £18 → 88% of live (£17.60) < floor*1.05 (£18.90); buffer wins."""
    r = compute_best_offer_thresholds(floor_gbp=18.0, live_price_gbp=20.0)
    assert r["auto_accept_gbp"] == 18.9
    # auto_decline: max(18, 0.72*20=14.4) = 18 (floor wins)
    assert r["auto_decline_gbp"] == 18.0


def test_high_live_price_uses_pct_thresholds() -> None:
    """Live £200, floor £18 → both percentages dominate floor."""
    r = compute_best_offer_thresholds(floor_gbp=18.0, live_price_gbp=200.0)
    assert r["auto_accept_gbp"] == 176.0  # 0.88 * 200
    assert r["auto_decline_gbp"] == 144.0  # 0.72 * 200


def test_custom_pct_overrides_default() -> None:
    r = compute_best_offer_thresholds(
        floor_gbp=10.0,
        live_price_gbp=100.0,
        auto_accept_pct=0.95,
        auto_decline_pct=0.80,
    )
    assert r["auto_accept_gbp"] == 95.0
    assert r["auto_decline_gbp"] == 80.0


def test_zero_floor_works() -> None:
    """Zero-cost baseline (config/fees.yaml default) — floor near zero is valid."""
    r = compute_best_offer_thresholds(floor_gbp=0.0, live_price_gbp=50.0)
    # max(0*1.05=0, 0.88*50=44) = 44; max(0, 0.72*50=36) = 36
    assert r["auto_accept_gbp"] == 44.0
    assert r["auto_decline_gbp"] == 36.0


def test_rationale_mentions_both_components() -> None:
    r = compute_best_offer_thresholds(floor_gbp=18.0, live_price_gbp=50.0)
    rationale = r["rationale"]
    assert "auto_accept" in rationale
    assert "auto_decline" in rationale
    assert "floor" in rationale


def test_below_floor_rationale_signal() -> None:
    """If for some pathological config auto_accept lands below floor, rationale flags it."""
    # negative percentages are not realistic but test the guardrail path
    r = compute_best_offer_thresholds(
        floor_gbp=100.0,
        live_price_gbp=10.0,
        auto_accept_pct=0.1,
        floor_buffer_pct=-0.5,  # max(50, 1) = 50; below floor 100
    )
    assert "below_floor" in r["rationale"] or r["auto_accept_gbp"] >= r["floor_gbp"]


def test_two_dp_rounding() -> None:
    r = compute_best_offer_thresholds(floor_gbp=18.0, live_price_gbp=49.99)
    # 0.88 * 49.99 = 43.9912 -> 43.99
    assert r["auto_accept_gbp"] == 43.99
