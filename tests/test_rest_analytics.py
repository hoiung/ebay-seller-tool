"""Unit tests for ebay.rest (Traffic Report + Post-Order returns + compute_return_rate)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ebay import oauth, rest

_FIXTURES = Path(__file__).parent / "fixtures"


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
    # Validation lives in the sync helper which both _raw and parsed paths share;
    # exercise via _raw to avoid double-parsing on a path that should never reach parse.
    with pytest.raises(ValueError, match="days must be"):
        _run(rest.fetch_traffic_report_raw(["111"], days=0))
    with pytest.raises(ValueError, match="days must be"):
        _run(rest.fetch_traffic_report_raw(["111"], days=91))


def test_traffic_report_empty_ids() -> None:
    with pytest.raises(ValueError, match="listing_ids"):
        _run(rest.fetch_traffic_report_raw([], days=30))


def test_traffic_report_raw_returns_ebay_wire_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #31 Phase 2 — ``fetch_traffic_report_raw`` returns the eBay
    JSON exactly as received. Filter-string construction is the AP #18
    contract assertion."""
    fake_payload = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
                {"key": "TRANSACTION"},
                {"key": "SALES_CONVERSION_RATE"},
            ]
        },
        "records": [
            {
                "dimensionValues": [{"value": "111", "applicable": True}],
                "metricValues": [
                    {"value": 500, "applicable": True},
                    {"value": 20, "applicable": True},
                    {"value": 2, "applicable": True},
                    {"value": 0.1, "applicable": True},
                ],
            }
        ],
    }
    fake_client = _fake_client_with_response(200, fake_payload)
    with patch("ebay.rest.get_oauth_session", return_value=fake_client):
        result = _run(rest.fetch_traffic_report_raw(["111", "222"], days=30))
    assert "records" in result
    assert "header" in result
    # AP #18: verify filter construction
    call_args = fake_client.get.call_args
    params = call_args.kwargs.get("params") or call_args.args[1]
    assert "listing_ids:{111|222}" in params["filter"]
    assert "marketplace_ids:{EBAY_GB}" in params["filter"]


def test_traffic_report_parsed_default_returns_decoded_shape() -> None:
    """Issue #31 Phase 2 — ``fetch_traffic_report`` is parsed-by-default.
    Same upstream payload as the raw test; assertion is on the parsed
    aggregate keys, NOT the raw eBay wire shape."""
    fake_payload = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
                {"key": "TRANSACTION"},
                {"key": "SALES_CONVERSION_RATE"},
            ]
        },
        "records": [
            {
                "dimensionValues": [{"value": "111", "applicable": True}],
                "metricValues": [
                    {"value": 500, "applicable": True},
                    {"value": 20, "applicable": True},
                    {"value": 2, "applicable": True},
                    {"value": 0.1, "applicable": True},
                ],
            }
        ],
    }
    fake_client = _fake_client_with_response(200, fake_payload)
    with patch("ebay.rest.get_oauth_session", return_value=fake_client):
        result = _run(rest.fetch_traffic_report(["111", "222"], days=30))
    # Parsed-by-default contract: callers receive the decoded aggregate
    # without having to know about parse_traffic_report_response.
    assert result["impressions"] == 500
    assert result["views"] == 20
    assert result["transactions"] == 2
    assert result["ctr_pct"] == 4.0
    assert result["sales_conversion_rate_pct"] == 10.0
    # Raw eBay shape NOT exposed at this surface.
    assert "header" not in result
    assert "records" not in result


def test_traffic_report_real_fixture_parses() -> None:
    """AC-1.4: real live-probe response for item 999000000001.

    Fixture captured 2026-04-24 against the live eBay Analytics API.
    Confirms the parser decodes the real response shape correctly.
    """
    with open(_FIXTURES / "traffic_report_real.json") as f:
        real = json.load(f)
    summary = rest.parse_traffic_report_response(real)
    assert summary["impressions"] == 3474
    assert summary["views"] == 76
    assert summary["transactions"] == 5
    assert summary["ctr_pct"] == 2.19  # 100 * 76 / 3474
    assert summary["sales_conversion_rate_pct"] == 6.0  # 0.06 * 100
    # Phase 1.3.3 — 4 new aggregate signals from search/store breakdown
    assert summary["search_impression_share_pct"] == 100.0
    assert summary["store_impression_share_pct"] == 0.0
    assert summary["search_view_share_pct"] == 57.89  # 100 * 44 / 76
    assert summary["organic_search_exposure_pct"] == 100.0  # synonym of search_imp_share
    assert summary["records_count"] == 1
    assert summary["per_listing"][0]["listing_id"] == "999000000001"


def test_parse_traffic_report_empty_records() -> None:
    """Empty records → zero aggregates + None rates, no exception."""
    summary = rest.parse_traffic_report_response({"header": {"metrics": []}, "records": []})
    assert summary["impressions"] == 0
    assert summary["views"] == 0
    assert summary["transactions"] == 0
    assert summary["ctr_pct"] is None
    assert summary["sales_conversion_rate_pct"] is None
    # Phase 1.3.1 — new aggregate signals also None when no impressions/views
    assert summary["search_impression_share_pct"] is None
    assert summary["store_impression_share_pct"] is None
    assert summary["search_view_share_pct"] is None
    assert summary["organic_search_exposure_pct"] is None
    assert summary["records_count"] == 0
    assert summary["per_listing"] == []


def test_parse_traffic_report_missing_dimension_value() -> None:
    """Regression (Stage 5 L1-E): dim_vals=[{}] (value key absent) yields
    listing_id=None, NOT the string 'None'.

    Pre-fix the parser did `str(dim_vals[0].get("value"))` which coerced None
    to the string "None", silently corrupting per_listing output.
    """
    payload = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
            ]
        },
        "records": [
            {
                # dimensionValues exists but value key missing
                "dimensionValues": [{}],
                "metricValues": [
                    {"value": 10, "applicable": True},
                    {"value": 2, "applicable": True},
                ],
            }
        ],
    }
    summary = rest.parse_traffic_report_response(payload)
    assert summary["per_listing"][0]["listing_id"] is None
    # Python None, not the string "None"
    assert summary["per_listing"][0]["listing_id"] != "None"


def test_parse_traffic_report_emits_abbreviated_aliases() -> None:
    """Issue #31 Phase 3 — parse output emits {imp, tx_count, conv_pct}
    aliases of the canonical descriptive keys, so the ebay-ops Fetchers
    Protocol consumer no longer hand-translates."""
    payload = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
                {"key": "TRANSACTION"},
                {"key": "SALES_CONVERSION_RATE"},
            ]
        },
        "records": [
            {
                "dimensionValues": [{"value": "111", "applicable": True}],
                "metricValues": [
                    {"value": 500, "applicable": True},
                    {"value": 20, "applicable": True},
                    {"value": 2, "applicable": True},
                    {"value": 0.1, "applicable": True},
                ],
            }
        ],
    }
    summary = rest.parse_traffic_report_response(payload)
    # Aliases are byte-equal to their canonical counterparts — one-way
    # contract, no separate code path inside parse.
    assert summary["imp"] == summary["impressions"] == 500
    assert summary["tx_count"] == summary["transactions"] == 2
    assert summary["conv_pct"] == summary["sales_conversion_rate_pct"] == 10.0
    # `views` and `ctr_pct` were already aligned across both naming
    # conventions; just confirm they're the same single keys (no spurious
    # duplicate emission).
    assert summary["views"] == 20
    assert summary["ctr_pct"] == 4.0


def test_parse_traffic_report_per_listing_summary_aggregates_across_days() -> None:
    """Issue #31 Phase 3 — per_listing_summary rolls up per-(listing × day)
    records into a per-listing dict in abbreviated demo-style keys. Eliminates
    the manual aggregation block in ebay-ops/scripts/run_weekly_tune_live.py."""
    payload = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
                {"key": "TRANSACTION"},
                {"key": "SALES_CONVERSION_RATE"},
            ]
        },
        "records": [
            # Listing A — two days, sums to imp=300 views=15 tx=1, conv mean=8%
            {
                "dimensionValues": [{"value": "A"}],
                "metricValues": [
                    {"value": 100, "applicable": True},
                    {"value": 5, "applicable": True},
                    {"value": 0, "applicable": True},
                    {"value": 0.06, "applicable": True},
                ],
            },
            {
                "dimensionValues": [{"value": "A"}],
                "metricValues": [
                    {"value": 200, "applicable": True},
                    {"value": 10, "applicable": True},
                    {"value": 1, "applicable": True},
                    {"value": 0.10, "applicable": True},
                ],
            },
            # Listing B — single day, no conversion data
            {
                "dimensionValues": [{"value": "B"}],
                "metricValues": [
                    {"value": 50, "applicable": True},
                    {"value": 2, "applicable": True},
                    {"value": 0, "applicable": True},
                    {"value": 0.0, "applicable": False},
                ],
            },
            # Listing with missing id — must NOT pollute the summary dict.
            {
                "dimensionValues": [{}],
                "metricValues": [
                    {"value": 999, "applicable": True},
                    {"value": 999, "applicable": True},
                    {"value": 999, "applicable": True},
                    {"value": 0.0, "applicable": True},
                ],
            },
        ],
    }
    summary = rest.parse_traffic_report_response(payload)
    pls = summary["per_listing_summary"]
    assert "A" in pls and "B" in pls
    assert None not in pls and "" not in pls

    a = pls["A"]
    assert a["imp"] == 300
    assert a["views"] == 15
    assert a["tx_count"] == 1
    # CTR computed from rolled-up imp/views: 100*15/300 = 5.0
    assert a["ctr_pct"] == 5.0
    # Conversion mean across the two days: (6.0 + 10.0)/2 = 8.0
    assert a["conv_pct"] == 8.0

    b = pls["B"]
    assert b["imp"] == 50
    assert b["views"] == 2
    assert b["tx_count"] == 0
    assert b["ctr_pct"] == 4.0  # 100*2/50
    # SCR not applicable for listing B → conv_pct is None, NOT 0.0
    assert b["conv_pct"] is None


def test_parse_traffic_report_alias_round_trip_matches_demo_protocol() -> None:
    """Issue #31 Phase 3 — round-trip check: the abbreviated keys emitted
    by parse satisfy the ebay-ops Fetchers Protocol shape exactly
    ({imp, views, ctr_pct, conv_pct, tx_count}). No translator required."""
    payload = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
                {"key": "TRANSACTION"},
                {"key": "SALES_CONVERSION_RATE"},
            ]
        },
        "records": [
            {
                "dimensionValues": [{"value": "999"}],
                "metricValues": [
                    {"value": 1000, "applicable": True},
                    {"value": 50, "applicable": True},
                    {"value": 3, "applicable": True},
                    {"value": 0.06, "applicable": True},
                ],
            }
        ],
    }
    summary = rest.parse_traffic_report_response(payload)
    PROTOCOL_KEYS = {"imp", "views", "ctr_pct", "conv_pct", "tx_count"}
    # All five keys present at top level (overall aggregate).
    assert PROTOCOL_KEYS.issubset(summary.keys())
    # And on each per-listing summary entry.
    for lid, agg in summary["per_listing_summary"].items():
        missing = PROTOCOL_KEYS - set(agg.keys())
        assert not missing, f"per_listing_summary[{lid}] missing keys: {missing}"


def test_parse_traffic_report_non_applicable_filtered() -> None:
    """applicable=False metric values are coerced to None and excluded from sums."""
    payload = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
                {"key": "SALES_CONVERSION_RATE"},
            ]
        },
        "records": [
            {
                "dimensionValues": [{"value": "999"}],
                "metricValues": [
                    {"value": 100, "applicable": True},
                    {"value": 5, "applicable": True},
                    {"value": 0.5, "applicable": False},  # non-applicable SCR
                ],
            }
        ],
    }
    summary = rest.parse_traffic_report_response(payload)
    assert summary["impressions"] == 100
    assert summary["views"] == 5
    assert summary["sales_conversion_rate_pct"] is None  # non-applicable excluded


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
    # Post-Order uses IAF scheme (get_post_order_session), not OAuth Bearer.
    with patch("ebay.rest.get_post_order_session", return_value=fake_client):
        result = _run(rest.fetch_listing_returns(item_id="111", days=90))
    assert "returns" in result
    call = fake_client.get.call_args
    assert call.args[0] == "/post-order/v2/return/search"
    params = call.kwargs.get("params") or call.args[1]
    assert params["item_id"] == "111"
    assert params["limit"] == 50


def test_compute_return_rate_zero_sold() -> None:
    with (
        patch("ebay.selling.fetch_sold_listings") as mock_sold,
        patch("ebay.rest.fetch_listing_returns") as mock_returns,
    ):

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
    with (
        patch("ebay.selling.fetch_sold_listings") as mock_sold,
        patch("ebay.rest.fetch_listing_returns") as mock_returns,
    ):

        async def sold(**kwargs):
            # AP #18: assert kwargs propagated explicitly
            assert kwargs["days"] == 60  # min(90, 60) cap on GetMyeBaySelling window
            assert kwargs["per_page"] == 200
            return {
                "listings": [
                    {"item_id": "111", "quantity_sold": 10},
                    {"item_id": "999", "quantity_sold": 5},
                ]
            }

        async def returns(**kwargs):
            assert kwargs["item_id"] == "111"
            assert kwargs["days"] == 90
            return {
                "returns": [
                    {"reason": "NOT_AS_DESCRIBED", "sellerTotalRefund": {"value": "25.00"}},
                    {"reason": "DAMAGED", "sellerTotalRefund": {"value": "30.00"}},
                ]
            }

        mock_sold.side_effect = sold
        mock_returns.side_effect = returns
        result = _run(rest.compute_return_rate(item_id="111", days=90))

    assert result["units_sold"] == 10
    assert result["returns_opened"] == 2
    assert result["return_rate_pct"] == 20.0
    assert result["return_reasons_dict"] == {"NOT_AS_DESCRIBED": 1, "DAMAGED": 1}
    assert result["total_refunded_gbp"] == 55.0
    # postage_loss = 2 returns × (3.50 outbound + 3.50 return) = 14.00
    assert result["estimated_postage_loss_gbp"] == 14.0
