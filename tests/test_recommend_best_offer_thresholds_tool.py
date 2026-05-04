"""Tests for server.recommend_best_offer_thresholds (G-NEW-1 MCP wrapper).

Covers:
- Missing item_id refused.
- Item-not-found refused with explicit error.
- Returns standalone shape (item_id, live_price_gbp, floor_gbp,
  auto_accept_gbp, auto_decline_gbp, return_rate_source, rationale).
- Floor source threaded through from _measure_or_default_floor.
- Custom auto_accept_pct / auto_decline_pct override config defaults.

Issue #16 (2026-05-02): MCP tool defaults shifted from 0.88/0.72 hardcoded
to None → config-driven (operator-locked 0.925/0.75 round-down). Default
behaviour now agrees with the autonomous responder. Test
test_returns_canonical_shape_with_default_pcts updated to pin the new
config-driven values; test_returns_canonical_shape_with_legacy_88_72_pcts
added for the historical-defaults path (explicit kwargs).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import server


def _run(coro):
    return asyncio.run(coro)


def _fake_item(price: str = "50.00") -> SimpleNamespace:
    return SimpleNamespace(
        reply=SimpleNamespace(
            Item=SimpleNamespace(
                ItemID="999",
                Title="t",
                SellingStatus=SimpleNamespace(
                    CurrentPrice=SimpleNamespace(value=price, _currencyID="GBP"),
                    QuantitySold="0",
                ),
                Quantity="1",
                QuantityAvailable="1",
                ListingDetails=SimpleNamespace(
                    ViewItemURL="https://www.ebay.co.uk/itm/999",
                    StartTime="2026-04-01T10:00:00Z",
                    EndTime="2026-05-01T10:00:00Z",
                    RelistCount="0",
                ),
                BestOfferEnabled="false",
                BestOfferCount="0",
                QuestionCount="0",
                WatchCount="0",
                HitCount="0",
                ConditionID="3000",
                ConditionDisplayName="Used",
                PrimaryCategory=SimpleNamespace(
                    CategoryID="56083", CategoryName="Internal Hard Disk Drives"
                ),
                Description="d",
                ShippingDetails=None,
                ReturnPolicy=None,
                PictureDetails=None,
                ItemSpecifics=SimpleNamespace(
                    NameValueList=[SimpleNamespace(Name="Brand", Value="Seagate")]
                ),
            )
        )
    )


def test_blank_item_id_refused() -> None:
    raw = _run(server.recommend_best_offer_thresholds(item_id="   "))
    body = json.loads(raw)
    assert "error" in body
    assert "item_id required" in body["error"]


def test_item_not_found_refused() -> None:
    not_found = SimpleNamespace(reply=SimpleNamespace(Item=None))
    with patch("server.execute_with_retry", side_effect=[not_found]):
        raw = _run(server.recommend_best_offer_thresholds(item_id="999"))
    body = json.loads(raw)
    assert "error" in body
    assert "not found" in body["error"]


def test_returns_canonical_shape_with_default_pcts() -> None:
    """Issue #30: default quantity=1 inherits qty_tiers[1] = 0.95.
    Pre-#16 hardcoded 0.88/0.72 → 44.0/36.0; #16 config-driven 0.925/0.75 → 46/37;
    #30 qty-tiered (qty=1 default) → 47/37 (floor(0.95 * 50) = 47).
    """
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("50.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 18.0,
                "suggested_ceiling_gbp": 30.0,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        raw = _run(server.recommend_best_offer_thresholds(item_id="999"))
    body = json.loads(raw)
    assert body["item_id"] == "999"
    assert body["live_price_gbp"] == 50.0
    assert body["floor_gbp"] == 18.0
    assert body["quantity"] == 1  # default qty echo
    # Post-#30 qty=1 tier: floor(0.95 * 50) = 47, floor(0.75 * 50) = 37
    assert body["auto_accept_gbp"] == 47
    assert body["auto_decline_gbp"] == 37
    assert body["return_rate_source"] == "default"
    assert "qty=1" in body["rationale"]


# ---------------------------------------------------------------------------
# Issue #30 AC2.5 — quantity parameter dispatch (4+ tests)
# ---------------------------------------------------------------------------


def test_quantity_1_returns_95pct_threshold() -> None:
    """quantity=1 → qty_tiers[1] = 0.95 → floor(0.95 * 50) = 47."""
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("50.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {"floor_gbp": 18.0, "suggested_ceiling_gbp": 30.0,
             "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0}},
            "default",
        )
        raw = _run(server.recommend_best_offer_thresholds(item_id="999", quantity=1))
    body = json.loads(raw)
    assert body["quantity"] == 1
    assert body["auto_accept_gbp"] == 47
    assert body["auto_decline_gbp"] == 37  # decline floor uniform across tiers
    assert "qty=1" in body["rationale"]


def test_quantity_2_returns_925pct_threshold() -> None:
    """quantity=2 → qty_tiers[2] = 0.925 → floor(0.925 * 50) = 46."""
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("50.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {"floor_gbp": 18.0, "suggested_ceiling_gbp": 30.0,
             "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0}},
            "default",
        )
        raw = _run(server.recommend_best_offer_thresholds(item_id="999", quantity=2))
    body = json.loads(raw)
    assert body["quantity"] == 2
    assert body["auto_accept_gbp"] == 46  # floor(0.925 * 50) = 46
    assert "qty=2" in body["rationale"]


def test_quantity_3_returns_default_90pct_threshold() -> None:
    """quantity=3 → not explicit key → 'default' = 0.90 → floor(0.90 * 50) = 45."""
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("50.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {"floor_gbp": 18.0, "suggested_ceiling_gbp": 30.0,
             "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0}},
            "default",
        )
        raw = _run(server.recommend_best_offer_thresholds(item_id="999", quantity=3))
    body = json.loads(raw)
    assert body["quantity"] == 3
    assert body["auto_accept_gbp"] == 45  # floor(0.90 * 50) = 45
    assert "default" in body["rationale"]


def test_quantity_omitted_defaults_to_qty1_tier() -> None:
    """Backward-compat: caller omits `quantity` → defaults to 1 → qty_tiers[1] tier.
    Same numbers as explicit quantity=1 (preserves existing-caller contract).
    """
    with (
        # Two GetItem calls (one per recommend invocation) → provide two fake items
        patch(
            "server.execute_with_retry",
            side_effect=[_fake_item("50.00"), _fake_item("50.00")],
        ),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {"floor_gbp": 18.0, "suggested_ceiling_gbp": 30.0,
             "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0}},
            "default",
        )
        raw_default = _run(server.recommend_best_offer_thresholds(item_id="999"))
        raw_explicit = _run(server.recommend_best_offer_thresholds(item_id="999", quantity=1))

    body_default = json.loads(raw_default)
    body_explicit = json.loads(raw_explicit)
    # Issue #30 worked example — multi-qty=3 cross-border-buyer at £105 listing:
    # Phase 5 sample invocation pins the qty=3 case; here we test qty=1 default.
    for key in ("auto_accept_gbp", "auto_decline_gbp", "floor_gbp", "rationale"):
        assert body_default[key] == body_explicit[key]
    assert body_default["quantity"] == 1


def test_response_has_full_canonical_keys() -> None:
    """Schema regression — return shape preserved across #30 (rationale text only changed)."""
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("50.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {"floor_gbp": 18.0, "suggested_ceiling_gbp": 30.0,
             "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0}},
            "default",
        )
        raw = _run(server.recommend_best_offer_thresholds(item_id="999", quantity=2))
    body = json.loads(raw)
    expected_keys = {
        "item_id", "live_price_gbp", "quantity", "return_rate_source",
        "auto_accept_gbp", "auto_decline_gbp", "floor_gbp", "rationale",
    }
    assert set(body.keys()) == expected_keys


def test_returns_canonical_shape_with_legacy_88_72_pcts() -> None:
    """Pin pre-#16 0.88/0.72 behaviour via explicit kwargs (kwargs win over config).

    Operator can still pin to historical band by passing explicit pcts. Shape
    is float at 2 dp because explicit kwargs path bypasses config's
    round_down_to_pound flag — operator gets the same shape pre-#16 callers got.
    """
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("50.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 18.0,
                "suggested_ceiling_gbp": 30.0,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        raw = _run(
            server.recommend_best_offer_thresholds(
                item_id="999", auto_accept_pct=0.88, auto_decline_pct=0.72
            )
        )
    body = json.loads(raw)
    # Note: round_down still applies (driven by config flag, not kwargs), so:
    # floor(0.88 * 50) = 44; floor(0.72 * 50) = 36 (still int but matches legacy values)
    assert body["auto_accept_gbp"] == 44
    assert body["auto_decline_gbp"] == 36


def test_threads_measured_return_rate_source() -> None:
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("50.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 18.0,
                "suggested_ceiling_gbp": 30.0,
                "inputs": {"return_rate": 0.15, "cogs_gbp": 0.0},
            },
            "measured (Phase 2, 90d)",
        )
        raw = _run(server.recommend_best_offer_thresholds(item_id="999"))
    body = json.loads(raw)
    assert body["return_rate_source"] == "measured (Phase 2, 90d)"


def test_custom_pcts_override_defaults() -> None:
    with (
        patch("server.execute_with_retry", side_effect=[_fake_item("100.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 18.0,
                "suggested_ceiling_gbp": 30.0,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        raw = _run(
            server.recommend_best_offer_thresholds(
                item_id="999", auto_accept_pct=0.95, auto_decline_pct=0.80
            )
        )
    body = json.loads(raw)
    assert body["auto_accept_gbp"] == 95.0
    assert body["auto_decline_gbp"] == 80.0
