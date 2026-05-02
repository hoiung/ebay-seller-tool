"""Unit tests for _evaluate_wrong_direction_raise helper (Issue #14 Phase 3 — AC3.5).

7 tests cover the WARN-fires path + every short-circuit gate:

  1. WARN fires on raise + units_sold>0 + BETWEEN_P25_P75 + comps available
  2. NO WARN when positional = BELOW_P25 (raising INTO mid-band is fine)
  3. NO WARN when units_sold = 0 (stalled listing — raise is sensible)
  4. NO WARN when comp_verdict = LONE_SUPPLIER (Stub #20)
  5. NO WARN when stock_clearance_exempt = True (Stub #21: qty>5 + DTS<3)
  6. NO WARN when comp_verdict = THIN_POOL (Stub #20 — 1≤kept≤3 unreliable)
  7. NO WARN when concentration.confidence == 'low' (Stub #19)

The helper is async and calls fetch_seller_transactions (ebay/selling.py)
+ fetch_competitor_prices (ebay/browse.py); tests patch BOTH at the
analytics module level (where the lazy imports happen).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ebay.analytics import _evaluate_wrong_direction_raise


def _run(coro):
    return asyncio.run(coro)


def _txns(item_id: str, units: int) -> dict:
    """Mock GetSellerTransactions reply with `units` units sold of item_id."""
    if units == 0:
        return {"transactions": []}
    return {
        "transactions": [
            {"item_id": item_id, "quantity_purchased": units, "transaction_id": "t1"}
        ]
    }


def _comp_result(
    verdict: str | None = None,
    p25: float | None = 30.0,
    p75: float | None = 50.0,
    confidence: str = "high",
) -> dict:
    """Mock fetch_competitor_prices return shape."""
    audit = {"concentration": {"confidence": confidence}}
    res: dict = {
        "p25": p25,
        "p75": p75,
        "median": (p25 + p75) / 2 if p25 and p75 else None,
        "audit": audit,
    }
    if verdict:
        res["verdict"] = verdict
    return res


def _item_full(
    mpn: str = "EG0300FBDBR",
    condition: str = "Used",
    watch_count: int = 7,
    quantity_available: int = 3,
    days_to_sell_median: int | None = None,
) -> dict:
    """Mock listing_to_dict shape with the fields the helper reads."""
    return {
        "specifics": {"MPN": [mpn], "Brand": ["HPE"]},
        "condition_name": condition,
        "watch_count": watch_count,
        "quantity_available": quantity_available,
        "days_to_sell_median": days_to_sell_median,
    }


def test_wrong_direction_fires_on_raise_with_velocity_and_comps() -> None:
    """AC3.5 #1 — happy-path: raise + units_sold>0 + BETWEEN_P25_P75 + comps available."""
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=5)
        # live_price=£25 sits BELOW p25=30 in this comp pool — but we want the
        # BETWEEN_P25_P75 case for the happy path. Use p25=20, p75=30 so £25
        # lands BETWEEN.
        mock_comps.return_value = _comp_result(p25=20.0, p75=30.0)

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=_item_full(),
            )
        )

    assert result is not None
    assert result["rule"] == "wrong_direction_raise_v1"
    assert result["old_price"] == 25.0
    assert result["new_price"] == 35.0
    assert result["units_sold"] == 5
    assert result["delta_pct"] == 40.0
    assert result["watch_count"] == 7
    assert "Raising risks killing velocity" in result["recommendation"]
    assert "restock-context" in result["recommendation"]


def test_wrong_direction_skips_when_positional_below_p25() -> None:
    """AC3.5 #2 — BELOW_P25 means raising INTO mid-band is sensible."""
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=5)
        # live_price=25 < p25=30 → BELOW_P25
        mock_comps.return_value = _comp_result(p25=30.0, p75=50.0)

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=_item_full(),
            )
        )

    assert result is None


def test_wrong_direction_skips_when_units_sold_zero() -> None:
    """AC3.5 #3 — no recent sales → raise is sensible (stalled listing)."""
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=0)
        mock_comps.return_value = _comp_result(p25=20.0, p75=30.0)

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=_item_full(),
            )
        )

    assert result is None
    # Comp pool was not even fetched (short-circuit on velocity).
    mock_comps.assert_not_called()


def test_wrong_direction_skips_when_comp_lone_supplier() -> None:
    """AC3.5 #4 — Stub #20 LONE_SUPPLIER: no usable market signal."""
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=5)
        mock_comps.return_value = _comp_result(verdict="LONE_SUPPLIER")

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=_item_full(),
            )
        )

    assert result is None


def test_wrong_direction_skips_when_stock_clearance_exempt() -> None:
    """AC3.5 #5 — Stub #21: qty>5 + DTS<3 = intentional clearance, not defect.

    The stock_clearance_exempt gate sits inside compute_under_pricing (called
    by the helper) and short-circuits BEFORE WARN fires. Configure item_full
    with quantity_available=10 and days_to_sell_median=1 so the exemption
    triggers.
    """
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=5)
        # BETWEEN_P25_P75 — would normally fire WARN, but stock_clearance_exempt overrides.
        mock_comps.return_value = _comp_result(p25=20.0, p75=30.0)

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=_item_full(quantity_available=10, days_to_sell_median=1),
            )
        )

    assert result is None


def test_wrong_direction_skips_when_thin_pool() -> None:
    """AC3.5 #6 — Stub #20 THIN_POOL: 1≤kept≤3 sample unreliable."""
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=5)
        mock_comps.return_value = _comp_result(verdict="THIN_POOL")

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=_item_full(),
            )
        )

    assert result is None


def test_wrong_direction_skips_when_concentration_low() -> None:
    """AC3.5 #7 — Stub #19: comp pool dominated by single seller (top_seller_pct>0.40).

    concentration.confidence='low' means the comp signal is unreliable; helper
    short-circuits with no WARN.
    """
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=5)
        mock_comps.return_value = _comp_result(p25=20.0, p75=30.0, confidence="low")

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=_item_full(),
            )
        )

    assert result is None


def test_wrong_direction_skips_when_mpn_missing() -> None:
    """AC3.1 contract: helper short-circuits when MPN can't be extracted from item_full.

    Without an MPN we can't fetch a comp pool to validate against, so the
    helper returns None instead of guessing.
    """
    with (
        patch("ebay.selling.fetch_seller_transactions", new_callable=AsyncMock) as mock_txns,
        patch("ebay.browse.fetch_competitor_prices", new_callable=AsyncMock) as mock_comps,
    ):
        mock_txns.return_value = _txns("999", units=5)
        item = _item_full()
        item["specifics"] = {"Brand": ["HPE"]}  # MPN absent
        mock_comps.return_value = _comp_result(p25=20.0, p75=30.0)

        result = _run(
            _evaluate_wrong_direction_raise(
                item_id="999",
                old_price=25.0,
                new_price=35.0,
                item_full=item,
            )
        )

    assert result is None
    mock_comps.assert_not_called()
