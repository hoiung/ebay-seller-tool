"""Regression tests for IncludeWatchCount=true on server-level GetItem sites (Issue #5 Phase 1).

`tests/test_selling_tools.py` only covers the GetMyeBaySelling fetchers in
ebay/selling.py. The five GetItem call sites in server.py need a separate
mock surface (patch at the server module level). This file covers the two
most critical sites:

  - get_listing_details (server.py line ~411)
  - analyse_listing     (server.py line ~1251 — THE call that produced the
                         "0 watchers across all listings" audit artefact)
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import server


def _reply(**kwargs: object) -> SimpleNamespace:
    """Build a fake ebaysdk Response wrapper matching the server-module contract."""
    return SimpleNamespace(reply=SimpleNamespace(**kwargs))


def _fake_item(item_id: str = "123") -> SimpleNamespace:
    """Minimal Item stub that listing_to_dict can serialise without attribute errors."""
    return SimpleNamespace(
        ItemID=item_id,
        Title="Seagate 2TB Test Listing",
        SellingStatus=SimpleNamespace(
            CurrentPrice=SimpleNamespace(value="35.00", _currencyID="GBP"),
            QuantitySold="0",
        ),
        Quantity="1",
        Description="<p>stub</p>",
        ConditionID="3000",
        ConditionDisplayName="Used",
        ConditionDescription="",
        SKU=None,
        ListingDetails=SimpleNamespace(StartTime=None, EndTime=None, ViewItemURL=None),
        PictureDetails=SimpleNamespace(PictureURL=[]),
        ItemSpecifics=SimpleNamespace(NameValueList=[]),
        ShippingDetails=None,
        ReturnPolicy=None,
        Location="Coventry",
        PostalCode="CV1 1AN",
        Country="GB",
        Site="UK",
        HitCount="0",
        WatchCount="7",
        BestOfferCount="0",
        PrimaryCategory=SimpleNamespace(
            CategoryID="56083", CategoryName="Internal Hard Disk Drives"
        ),
    )


def _run(coro):
    return asyncio.run(coro)


def test_get_listing_details_sends_include_watch_count() -> None:
    """server.py:~411 get_listing_details GetItem — AC 1.4."""
    with patch(
        "server.execute_with_retry",
        return_value=_reply(Item=_fake_item("999")),
    ) as mock_exec:
        # Reach the underlying tool coroutine through FastMCP's registry.
        tool = server.mcp._tool_manager._tools["get_listing_details"]
        result_json = _run(tool.fn(item_id="999"))

    # Response shape is preserved — listing_to_dict produced a dict.
    parsed = json.loads(result_json)
    assert "error" not in parsed

    # AP #18 explicit kwarg propagation — GetItem invoked with the flag.
    call_args = mock_exec.call_args
    assert call_args.args[0] == "GetItem"
    payload = call_args.args[1]
    assert payload["ItemID"] == "999"
    assert payload["DetailLevel"] == "ReturnAll"
    assert payload["IncludeItemSpecifics"] == "true"
    # Issue #5 Phase 1 regression: the opt-in flag is the whole point.
    assert payload["IncludeWatchCount"] == "true"


def test_analyse_listing_sends_include_watch_count() -> None:
    """server.py:~1251 analyse_listing GetItem — THE audit-artefact call site.

    analyse_listing's first inner call is GetItem; subsequent calls
    (fetch_seller_transactions etc.) would fail on our minimal stub,
    so we assert on the first call's kwargs only — that is sufficient
    for the Phase 1 flag-propagation contract.
    """
    with patch(
        "server.execute_with_retry",
        return_value=_reply(Item=_fake_item("888")),
    ) as mock_exec:
        tool = server.mcp._tool_manager._tools["analyse_listing"]
        # Expect the call to fail somewhere after GetItem because our stub
        # doesn't mock fetch_seller_transactions — but the GetItem call
        # was recorded before the failure, so the assertion still holds.
        try:
            _run(tool.fn(item_id="888"))
        except Exception:
            pass

    # First recorded call is GetItem with the flag.
    first_call = mock_exec.call_args_list[0]
    assert first_call.args[0] == "GetItem"
    payload = first_call.args[1]
    assert payload["ItemID"] == "888"
    assert payload["DetailLevel"] == "ReturnAll"
    assert payload["IncludeItemSpecifics"] == "true"
    # Issue #5 Phase 1 regression — the audit artefact's root cause.
    assert payload["IncludeWatchCount"] == "true"


def test_analyse_listing_phase2_backfills_views(tmp_path, monkeypatch) -> None:
    """AC-5.4: full E2E wiring — Phase 2 backfill + absolute-signal STABLE.

    Mocks the entire fetcher swarm analyse_listing depends on. Asserts:
      - funnel.views == 76 (backfilled from Analytics API, not HitCount=0)
      - funnel.watchers_per_100_views ≈ 9.21 (100 * 7 / 76)
      - rank_health_status == "STABLE"
      - diagnosis text does NOT contain 'Low views' or 'Rewrite title'
    Regression guard for the 999000000001 failure.

    Also asserts Phase 5.2.1: snapshot is written with analysis_baseline event.
    """
    # Phase 5.2.3 — redirect snapshot path so test doesn't pollute real file.
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    async def fake_fetch_seller_transactions(**_):
        return {"transactions": []}

    async def fake_fetch_listing_feedback(**_):
        return {"entries": []}

    async def fake_fetch_listing_cases(**_):
        return {"open_cases": 0}

    async def fake_fetch_sold_listings(**_):
        return {"listings": []}

    async def fake_fetch_unsold_listings(**_):
        return {"listings": []}

    async def fake_fetch_traffic_report(*_, **__):
        return {
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
                    "dimensionValues": [{"value": "999", "applicable": True}],
                    "metricValues": [
                        {"value": 3474, "applicable": True},
                        {"value": 76, "applicable": True},
                        {"value": 5, "applicable": True},
                        {"value": 0.06, "applicable": True},
                    ],
                }
            ],
        }

    async def fake_rest_compute_return_rate(**_):
        return {"return_rate_pct": None}

    item_stub = _fake_item("999")
    # Raise items to match the 999000000001 scenario
    item_stub.WatchCount = "7"
    item_stub.SellingStatus.QuantitySold = "5"
    # Listing is 30 days old so days_on_site >= 14 (STABLE eligibility)
    from datetime import datetime, timedelta, timezone

    start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item_stub.ListingDetails = SimpleNamespace(
        StartTime=start_30d, EndTime=None, ViewItemURL="https://ebay.co.uk/itm/999"
    )

    with (
        patch("server.execute_with_retry", return_value=_reply(Item=item_stub)),
        patch("server.fetch_seller_transactions", side_effect=fake_fetch_seller_transactions),
        patch("server.fetch_listing_feedback", side_effect=fake_fetch_listing_feedback),
        patch("server.fetch_listing_cases", side_effect=fake_fetch_listing_cases),
        patch("server.fetch_sold_listings", side_effect=fake_fetch_sold_listings),
        patch("server.fetch_unsold_listings", side_effect=fake_fetch_unsold_listings),
        patch("server.fetch_traffic_report", side_effect=fake_fetch_traffic_report),
        patch("server.rest_compute_return_rate", side_effect=fake_rest_compute_return_rate),
    ):
        tool = server.mcp._tool_manager._tools["analyse_listing"]
        result_json = _run(tool.fn(item_id="999"))

    parsed = json.loads(result_json)
    assert "error" not in parsed, f"unexpected error: {parsed}"

    # Bug 0.3 — Phase 2 success path surfaces phase2_available=True
    assert parsed["phase2_available"] is True

    # Phase 2 backfill
    assert parsed["funnel"]["views"] == 76
    assert parsed["funnel"]["impressions"] == 3474
    assert parsed["funnel"]["watchers_per_100_views"] == 9.21  # 100 * 7 / 76
    assert parsed["funnel"]["conversion_rate_pct_approx"] == 6.58  # 100 * 5 / 76
    assert parsed["funnel"]["ctr_pct"] == 2.19  # 100 * 76 / 3474

    # Rank + diagnosis
    assert parsed["rank_health_status"] == "STABLE"
    assert "Low views" not in parsed["diagnosis"]
    assert (
        parsed["recommended_action"] is None or "Rewrite title" not in parsed["recommended_action"]
    )

    # #17 fix (Stage 1 L2 F2): days_on_site key present in response. Item built
    # with StartTime=30d ago, so value should be 29 or 30 (depending on sub-day
    # rounding in days_on_site computation at listings.py:182-188).
    assert "days_on_site" in parsed
    assert parsed["days_on_site"] in (29, 30)

    # G-NEW-3: days-to-sell distribution surfaced. With empty transactions list,
    # n_samples=0 and percentiles are None.
    assert parsed["days_to_sell_n_samples"] == 0
    assert parsed["days_to_sell_p25"] is None
    assert parsed["days_to_sell_p50"] is None
    assert parsed["days_to_sell_p75"] is None
    # Backwards compat: median preserved
    assert parsed["days_to_sell_median"] is None

    # Phase 5.2.1 — happy path emits analysis_baseline snapshot.
    assert snap_path.exists(), "analyse_listing should have written analysis_baseline snapshot"
    lines = snap_path.read_text().strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "analysis_baseline"
    assert row["item_id"] == "999"
    assert row["price_gbp"] == 35.0
    assert row["source"] == "analyse_listing"


def test_analyse_listing_phase2_unavailable_returns_data_gap(tmp_path, monkeypatch) -> None:
    """AC-5.4 companion: Phase 2 unavailable + strong Phase 1 signals → data-gap diagnosis.

    Simulates OAuth-unconfigured deployment: fetch_traffic_report raises
    (e.g. PermissionError). Should degrade gracefully to data-gap diagnosis
    with absolute-signal STABLE, not false-alarm 'Low views'.

    Also asserts Phase 5.2.1: snapshot is NOT written when phase2_available=False.
    """
    # Phase 5.2.3 — redirect snapshot path. Phase2 unavailable → no snapshot expected.
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    async def fake_raises(*_, **__):
        raise RuntimeError("OAuth not configured")

    async def fake_empty(**_):
        return {"transactions": [], "entries": [], "listings": [], "open_cases": 0}

    async def fake_transactions(**_):
        return {"transactions": []}

    async def fake_feedback(**_):
        return {"entries": []}

    async def fake_cases(**_):
        return {"open_cases": 0}

    async def fake_sold_unsold(**_):
        return {"listings": []}

    async def fake_return_rate_raises(**_):
        raise RuntimeError("OAuth not configured")

    item_stub = _fake_item("999")
    item_stub.WatchCount = "7"
    item_stub.SellingStatus.QuantitySold = "5"
    from datetime import datetime, timedelta, timezone

    start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item_stub.ListingDetails = SimpleNamespace(
        StartTime=start_30d, EndTime=None, ViewItemURL="https://ebay.co.uk/itm/999"
    )

    with (
        patch("server.execute_with_retry", return_value=_reply(Item=item_stub)),
        patch("server.fetch_seller_transactions", side_effect=fake_transactions),
        patch("server.fetch_listing_feedback", side_effect=fake_feedback),
        patch("server.fetch_listing_cases", side_effect=fake_cases),
        patch("server.fetch_sold_listings", side_effect=fake_sold_unsold),
        patch("server.fetch_unsold_listings", side_effect=fake_sold_unsold),
        patch("server.fetch_traffic_report", side_effect=fake_raises),
        patch("server.rest_compute_return_rate", side_effect=fake_return_rate_raises),
    ):
        tool = server.mcp._tool_manager._tools["analyse_listing"]
        result_json = _run(tool.fn(item_id="999"))

    parsed = json.loads(result_json)
    assert "error" not in parsed, f"unexpected error: {parsed}"

    # Bug 0.3 — Phase 2 raised → phase2_available=False (no silent success)
    assert parsed["phase2_available"] is False

    # Phase 1 only — view_count=None flows through
    assert parsed["funnel"]["views"] is None
    assert parsed["funnel"]["impressions"] is None

    # Rank: absolute-signal fallback → STABLE
    assert parsed["rank_health_status"] == "STABLE"

    # Diagnosis: data-gap branch (NOT 'Low views')
    assert "Data gap" in parsed["diagnosis"]
    assert "Low views" not in parsed["diagnosis"]
    assert parsed["recommended_action"] is None

    # Phase 5.2.1 — Phase 2 unavailable → NO snapshot written (gated on phase2_available).
    assert not snap_path.exists(), "snapshot should NOT be written when phase2_available=False"


def test_analyse_listing_days_to_sell_distribution(tmp_path, monkeypatch) -> None:
    """G-NEW-3: analyse_listing surfaces days-to-sell p25/p50/p75/n_samples
    computed from per-item seller transactions. Verifies linear-interpolation
    percentile math against a known input distribution.

    Distribution: 4 transactions with days_to_sell = [2, 5, 7, 12]
      Sorted: [2, 5, 7, 12], n=4
      p25 = (n-1)*0.25 = 0.75 → idx 0..1, frac 0.75 → 2 + 0.75*(5-2) = 4.25
      p50 = (n-1)*0.50 = 1.5  → idx 1..2, frac 0.5  → 5 + 0.5*(7-5)  = 6.0
      p75 = (n-1)*0.75 = 2.25 → idx 2..3, frac 0.25 → 7 + 0.25*(12-7) = 8.25
      median (legacy alias of p50) = 6.0
    """
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    async def fake_fetch_seller_transactions(**_):
        return {
            "transactions": [
                {"item_id": "999", "days_to_sell": 2},
                {"item_id": "999", "days_to_sell": 5},
                {"item_id": "999", "days_to_sell": 7},
                {"item_id": "999", "days_to_sell": 12},
                {"item_id": "888", "days_to_sell": 100},  # different item — filtered out
                {"item_id": "999", "days_to_sell": None},  # null filtered out
            ]
        }

    async def fake_fetch_listing_feedback(**_):
        return {"entries": []}

    async def fake_fetch_listing_cases(**_):
        return {"open_cases": 0}

    async def fake_fetch_sold_listings(**_):
        return {"listings": []}

    async def fake_fetch_unsold_listings(**_):
        return {"listings": []}

    async def fake_raises(*_, **__):
        raise RuntimeError("phase 2 not relevant for this test")

    async def fake_return_rate_raises(**_):
        raise RuntimeError("not relevant")

    item_stub = _fake_item("999")
    from datetime import datetime, timedelta, timezone

    start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item_stub.ListingDetails = SimpleNamespace(
        StartTime=start_30d, EndTime=None, ViewItemURL="https://ebay.co.uk/itm/999"
    )

    with (
        patch("server.execute_with_retry", return_value=_reply(Item=item_stub)),
        patch("server.fetch_seller_transactions", side_effect=fake_fetch_seller_transactions),
        patch("server.fetch_listing_feedback", side_effect=fake_fetch_listing_feedback),
        patch("server.fetch_listing_cases", side_effect=fake_fetch_listing_cases),
        patch("server.fetch_sold_listings", side_effect=fake_fetch_sold_listings),
        patch("server.fetch_unsold_listings", side_effect=fake_fetch_unsold_listings),
        patch("server.fetch_traffic_report", side_effect=fake_raises),
        patch("server.rest_compute_return_rate", side_effect=fake_return_rate_raises),
    ):
        tool = server.mcp._tool_manager._tools["analyse_listing"]
        result_json = _run(tool.fn(item_id="999"))

    parsed = json.loads(result_json)
    assert "error" not in parsed

    # 4 valid txns for item 999 (item 888 + None excluded)
    assert parsed["days_to_sell_n_samples"] == 4

    # Linear-interp percentiles per docstring above
    assert parsed["days_to_sell_p25"] == 4.25
    assert parsed["days_to_sell_p50"] == 6.0
    assert parsed["days_to_sell_p75"] == 8.25

    # Backwards compat: median preserved (== p50)
    assert parsed["days_to_sell_median"] == 6.0
