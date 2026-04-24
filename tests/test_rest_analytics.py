"""Unit tests for ebay.rest (Traffic Report + Post-Order returns + compute_return_rate)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ebay import oauth, rest


def setup_function() -> None:
    oauth.reset_token_cache()


def _run(coro):
    return asyncio.run(coro)


def _fake_client_with_response(status: int, json_payload: object) -> MagicMock:
    """Build a MagicMock httpx.Client context-manager that returns a canned response."""
    client = MagicMock()
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.url = "https://api.ebay.com/fake"
    resp.text = "{}"
    resp.json.return_value = json_payload
    client.get.return_value = resp
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def test_traffic_report_days_bounds() -> None:
    with pytest.raises(ValueError, match="days must be"):
        _run(rest.fetch_traffic_report(["111"], days=0))
    with pytest.raises(ValueError, match="days must be"):
        _run(rest.fetch_traffic_report(["111"], days=91))


def test_traffic_report_empty_ids() -> None:
    with pytest.raises(ValueError, match="listing_ids"):
        _run(rest.fetch_traffic_report([], days=30))


def test_traffic_report_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _fake_client_with_response(
        200, {"records": [{"metrics": [{"metricKey": "LISTING_IMPRESSION_TOTAL", "value": 500}]}]}
    )
    with patch("ebay.rest.get_oauth_session", return_value=fake_client):
        result = _run(rest.fetch_traffic_report(["111", "222"], days=30))
    assert "records" in result
    # AP #18: verify filter construction
    call_args = fake_client.get.call_args
    params = call_args.kwargs.get("params") or call_args.args[1]
    assert "listing_ids:{111|222}" in params["filter"]
    assert "marketplace_ids:{EBAY_GB}" in params["filter"]


def test_fetch_listing_returns_requires_item_id() -> None:
    with pytest.raises(ValueError, match="item_id"):
        _run(rest.fetch_listing_returns(item_id=""))


def test_fetch_listing_returns_happy_path() -> None:
    fake_client = _fake_client_with_response(
        200,
        {
            "returns": [
                {
                    "return_id": "R1",
                    "reason": "NOT_AS_DESCRIBED",
                    "sellerTotalRefund": {"value": "25.00", "currency": "GBP"},
                    "state": "PENDING",
                }
            ]
        },
    )
    with patch("ebay.rest.get_oauth_session", return_value=fake_client):
        result = _run(rest.fetch_listing_returns(item_id="111", days=90))
    assert "returns" in result
    assert fake_client.get.call_args.args[0] == "/post-order/v2/return/search"


def test_compute_return_rate_zero_sold() -> None:
    with patch("ebay.selling.fetch_sold_listings") as mock_sold, patch(
        "ebay.rest.fetch_listing_returns"
    ) as mock_returns:
        async def sold_empty(**_):
            return {"listings": []}

        async def returns_empty(**_):
            return {"returns": []}

        mock_sold.side_effect = sold_empty
        mock_returns.side_effect = returns_empty
        result = _run(rest.compute_return_rate(item_id="111"))
    assert result["units_sold"] == 0
    assert result["return_rate_pct"] is None


def test_compute_return_rate_with_sales_and_returns() -> None:
    with patch("ebay.selling.fetch_sold_listings") as mock_sold, patch(
        "ebay.rest.fetch_listing_returns"
    ) as mock_returns:
        async def sold(**_):
            return {"listings": [
                {"item_id": "111", "quantity_sold": 10},
                {"item_id": "999", "quantity_sold": 5},
            ]}

        async def returns(**_):
            return {"returns": [
                {"reason": "NOT_AS_DESCRIBED", "sellerTotalRefund": {"value": "25.00"}},
                {"reason": "DAMAGED", "sellerTotalRefund": {"value": "30.00"}},
            ]}

        mock_sold.side_effect = sold
        mock_returns.side_effect = returns
        result = _run(rest.compute_return_rate(item_id="111", days=90))

    assert result["units_sold"] == 10
    assert result["returns_opened"] == 2
    assert result["return_rate_pct"] == 20.0
    assert result["return_reasons_dict"] == {"NOT_AS_DESCRIBED": 1, "DAMAGED": 1}
    assert result["total_refunded_gbp"] == 55.0
