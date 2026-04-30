"""Tests for server.recommend_best_offer_thresholds (G-NEW-1 MCP wrapper).

Covers:
- Missing item_id refused.
- Item-not-found refused with explicit error.
- Returns standalone shape (item_id, live_price_gbp, floor_gbp,
  auto_accept_gbp, auto_decline_gbp, return_rate_source, rationale).
- Floor source threaded through from _measure_or_default_floor.
- Custom auto_accept_pct / auto_decline_pct override default 0.88 / 0.72.
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
    assert body["auto_accept_gbp"] == 44.0
    assert body["auto_decline_gbp"] == 36.0
    assert body["return_rate_source"] == "default"
    assert "auto_accept" in body["rationale"]


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
            {"floor_gbp": 18.0, "suggested_ceiling_gbp": 30.0, "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0}},
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
