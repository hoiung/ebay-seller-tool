"""Phase 4 floor-price guardrail tests for update_listing (Issue #4 AC 4.3)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _run(coro):
    return asyncio.run(coro)


def _fake_get_item(price: str = "35.00") -> SimpleNamespace:
    """Build a GetItem response with minimal fields for update_listing path."""
    return SimpleNamespace(
        reply=SimpleNamespace(
            Item=SimpleNamespace(
                ItemID="999",
                Title="Seagate 2TB",
                SellingStatus=SimpleNamespace(
                    CurrentPrice=SimpleNamespace(value=price, _currencyID="GBP"),
                    QuantitySold="1",
                ),
                Quantity="1",
                QuantityAvailable="1",
                ListingDetails=SimpleNamespace(
                    ViewItemURL="https://www.ebay.co.uk/itm/999",
                    StartTime="2026-03-01T10:00:00Z",
                    EndTime="2026-04-01T10:00:00Z",
                    RelistCount="0",
                ),
                BestOfferCount="0",
                BestOfferEnabled="false",
                QuestionCount="0",
                WatchCount="0",
                HitCount="10",
                ConditionID="3000",
                ConditionDisplayName="Used",
                PrimaryCategory=SimpleNamespace(
                    CategoryID="56083", CategoryName="Internal Hard Disk Drives"
                ),
                Description="desc",
                ShippingDetails=None,
                ReturnPolicy=None,
                PictureDetails=None,
                ItemSpecifics=SimpleNamespace(
                    NameValueList=[
                        SimpleNamespace(Name="Brand", Value="Seagate"),
                        SimpleNamespace(Name="MPN", Value="ST2000NM"),
                    ]
                ),
            )
        )
    )


def test_update_listing_writes_price_change_snapshot(tmp_path, monkeypatch) -> None:
    """Phase 5.2.2 — update_listing emits price_change snapshot when price changes.

    Full E2E flow: GetItem (current) → guardrail check → ReviseFixedPriceItem
    → GetItem (verify) → assert JSONL has one price_change event.
    """
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    from server import update_listing

    # 3 sequential execute_with_retry calls: GetItem (before), ReviseFixedPriceItem,
    # GetItem (verify). The Revise response shape is irrelevant here.
    with (
        patch(
            "server.execute_with_retry",
            side_effect=[
                _fake_get_item("30.00"),  # before: price 30
                SimpleNamespace(reply=SimpleNamespace()),  # ReviseFixedPriceItem ack
                _fake_get_item("35.00"),  # after: price 35
            ],
        ),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 7.94,
                "suggested_ceiling_gbp": 50.00,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        result = _run(update_listing(item_id="999", price=35.0, dry_run=False))

    body = json.loads(result)
    assert body.get("success") is True

    assert snap_path.exists(), "update_listing should have appended price_change snapshot"
    lines = snap_path.read_text().strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "price_change"
    assert row["item_id"] == "999"
    assert row["price_gbp"] == 35.0
    assert row["old_price_gbp"] == 30.0
    assert row["source"] == "update_listing"


def test_update_listing_no_snapshot_when_price_unchanged(tmp_path, monkeypatch) -> None:
    """Phase 5.2.2 — same price → no price_change snapshot."""
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    from server import update_listing

    # Updating only the title, NOT the price.
    with (
        patch(
            "server.execute_with_retry",
            side_effect=[
                _fake_get_item("35.00"),
                SimpleNamespace(reply=SimpleNamespace()),
                _fake_get_item("35.00"),
            ],
        ),
    ):
        result = _run(update_listing(item_id="999", title="New title", dry_run=False))

    body = json.loads(result)
    assert body.get("success") is True
    # No price field changed → no price_change snapshot.
    assert not snap_path.exists()


def test_guardrail_rejects_below_floor() -> None:
    from server import update_listing

    with (
        patch("server.execute_with_retry", side_effect=[_fake_get_item()]) as _,
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 7.94,
                "suggested_ceiling_gbp": 11.91,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        result = _run(update_listing(item_id="999", price=5.00, dry_run=False))
    body = json.loads(result)
    assert "error" in body
    assert "below floor" in body["error"]
    assert body["floor_gbp"] == 7.94
    assert body["requested_price"] == 5.00


def test_guardrail_accepts_at_exact_floor() -> None:
    from server import update_listing

    # current price 100.00, test price 7.94 - diff triggers
    with (
        patch("server.execute_with_retry", side_effect=[_fake_get_item("100.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 7.94,
                "suggested_ceiling_gbp": 11.91,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        result = _run(update_listing(item_id="999", price=7.94, dry_run=True))
    body = json.loads(result)
    assert body["dry_run"] is True
    assert body["floor_gbp"] == 7.94
    assert "OK" in body["price_verdict"]


def test_guardrail_dry_run_no_current_analysis() -> None:
    from server import update_listing

    with (
        patch("server.execute_with_retry", side_effect=[_fake_get_item("100.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 7.94,
                "suggested_ceiling_gbp": 11.91,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        result = _run(update_listing(item_id="999", price=35.0, dry_run=True))
    body = json.loads(result)
    assert "floor_gbp" in body
    assert "current_analysis" not in body


def test_guardrail_dry_run_echoes_current_analysis() -> None:
    from server import update_listing

    analysis_input = {"item_id": "999", "rank_health_status": "STABLE"}
    with (
        patch("server.execute_with_retry", side_effect=[_fake_get_item("100.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 7.94,
                "suggested_ceiling_gbp": 11.91,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        result = _run(
            update_listing(item_id="999", price=35.0, dry_run=True, current_analysis=analysis_input)
        )
    body = json.loads(result)
    assert body["current_analysis"] == analysis_input


def test_guardrail_error_message_cites_source() -> None:
    from server import update_listing

    with (
        patch("server.execute_with_retry", side_effect=[_fake_get_item("100.00")]),
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 8.50,
                "suggested_ceiling_gbp": 12.75,
                "inputs": {"return_rate": 0.15, "cogs_gbp": 0.0},
            },
            "measured (Phase 2, 90d)",
        )
        result = _run(update_listing(item_id="999", price=5.00, dry_run=False))
    body = json.loads(result)
    err = body["error"]
    assert err.startswith("Price £5.00 below floor £8.50"), f"unexpected prefix: {err!r}"
    assert "measured (Phase 2, 90d)" in err
    assert "15.0%" in err
    assert "COGS £0.00" in err


def test_guardrail_additive_only_21_field_invariance() -> None:
    """AC 4.3c: guardrail must not reach ReviseFixedPriceItem when rejecting.

    Proves the guardrail is additive-only by asserting that a below-floor price
    never triggers the Revise path, so ItemSpecifics cannot possibly be mutated.
    """
    from server import update_listing

    with (
        patch("server.execute_with_retry", side_effect=[_fake_get_item("100.00")]) as mock_exec,
        patch("server._measure_or_default_floor", new_callable=AsyncMock) as mock_floor,
    ):
        mock_floor.return_value = (
            {
                "floor_gbp": 7.94,
                "suggested_ceiling_gbp": 11.91,
                "inputs": {"return_rate": 0.10, "cogs_gbp": 0.0},
            },
            "default",
        )
        _run(update_listing(item_id="999", price=5.00, dry_run=False))
    # Only the initial GetItem fetch happened — no Revise call.
    assert mock_exec.call_count == 1
    assert mock_exec.call_args_list[0].args[0] == "GetItem"
    revise_calls = [c for c in mock_exec.call_args_list if c.args[0] == "ReviseFixedPriceItem"]
    assert revise_calls == []
