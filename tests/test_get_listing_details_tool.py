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
        PrimaryCategory=SimpleNamespace(CategoryID="56083", CategoryName="Internal Hard Disk Drives"),
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
