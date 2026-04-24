"""Unit tests for ebay.selling Trading API wrappers (Issue #4 Phase 1).

Mocks execute_with_retry to return canonical eBay responses. Every mock
assertion reads call_args.args[1] (the payload dict) explicitly to prove
arg propagation (AP #18). execute_with_retry is called positionally — no
**kwargs path exists in production, so args[1] is the only capture point;
the mock does not accept **kwargs and cannot silently discard fields.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ebay.selling import (
    fetch_listing_cases,
    fetch_listing_feedback,
    fetch_seller_transactions,
    fetch_sold_listings,
    fetch_unsold_listings,
)


def _reply(**kwargs: object) -> SimpleNamespace:
    """Build a fake ebaysdk Response wrapper."""
    return SimpleNamespace(reply=SimpleNamespace(**kwargs))


def _run(coro):
    return asyncio.run(coro)


def test_fetch_sold_listings_happy_path() -> None:
    sold_list = SimpleNamespace(
        PaginationResult=SimpleNamespace(TotalNumberOfEntries="2"),
        OrderTransactionArray=SimpleNamespace(
            OrderTransaction=[
                SimpleNamespace(
                    Transaction=SimpleNamespace(
                        Item=SimpleNamespace(
                            ItemID="111",
                            Title="Seagate 2TB",
                            ListingDetails=SimpleNamespace(
                                StartTime="2026-03-01T10:00:00Z",
                                EndTime="2026-03-10T10:00:00Z",
                            ),
                            BestOfferCount="0",
                            WatchCount="3",
                        ),
                        TransactionPrice=SimpleNamespace(value="25.00", _currencyID="GBP"),
                        QuantityPurchased="1",
                    )
                )
            ]
        ),
    )
    with patch("ebay.selling.execute_with_retry", return_value=_reply(SoldList=sold_list)) as mock:
        result = _run(fetch_sold_listings(days=30, page=1, per_page=25))

    assert result["total"] == 2
    assert len(result["listings"]) == 1
    l = result["listings"][0]
    assert l["item_id"] == "111"
    assert l["sold_price"] == "25.00"
    assert l["days_live"] == 9
    assert l["watch_count"] == 3

    # AP #18 explicit kwarg propagation
    call_args = mock.call_args
    assert call_args.args[0] == "GetMyeBaySelling"
    assert call_args.args[1]["SoldList"]["DurationInDays"] == 30
    assert call_args.args[1]["SoldList"]["Pagination"]["EntriesPerPage"] == 25
    # Issue #5 Phase 1 regression: IncludeWatchCount opt-in flag MUST be present.
    # DetailLevel=ReturnAll does NOT include WatchCount — explicit flag required.
    assert call_args.args[1]["SoldList"]["IncludeWatchCount"] == "true"


def test_fetch_sold_listings_days_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match=r"days must be"):
        _run(fetch_sold_listings(days=0))
    with pytest.raises(ValueError, match=r"days must be"):
        _run(fetch_sold_listings(days=61))


def test_fetch_sold_listings_per_page_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match=r"per_page"):
        _run(fetch_sold_listings(days=30, per_page=0))
    with pytest.raises(ValueError, match=r"per_page"):
        _run(fetch_sold_listings(days=30, per_page=201))


def test_fetch_sold_listings_empty_response() -> None:
    with patch("ebay.selling.execute_with_retry", return_value=_reply(SoldList=None)):
        result = _run(fetch_sold_listings(days=30))
    assert result["total"] == 0
    assert result["listings"] == []


def test_fetch_unsold_listings_happy_path() -> None:
    unsold_list = SimpleNamespace(
        PaginationResult=SimpleNamespace(TotalNumberOfEntries="1"),
        ItemArray=SimpleNamespace(
            Item=SimpleNamespace(
                ItemID="222",
                Title="Toshiba 1TB",
                ListingDetails=SimpleNamespace(
                    StartTime="2026-02-01T10:00:00Z",
                    EndTime="2026-04-01T10:00:00Z",
                ),
                SellingStatus=SimpleNamespace(CurrentPrice=SimpleNamespace(value="20.00", _currencyID="GBP")),
                BestOfferCount="0",
                WatchCount="1",
            )
        ),
    )
    with patch("ebay.selling.execute_with_retry", return_value=_reply(UnsoldList=unsold_list)) as mock:
        result = _run(fetch_unsold_listings(days=60))
    assert result["total"] == 1
    assert result["listings"][0]["item_id"] == "222"
    assert result["listings"][0]["days_live"] == 59
    assert mock.call_args.args[1]["UnsoldList"]["DurationInDays"] == 60
    # Issue #5 Phase 1 regression: IncludeWatchCount opt-in flag MUST be present.
    assert mock.call_args.args[1]["UnsoldList"]["IncludeWatchCount"] == "true"


def test_fetch_seller_transactions_happy_path() -> None:
    txn_array = SimpleNamespace(
        Transaction=[
            SimpleNamespace(
                TransactionID="T1",
                Item=SimpleNamespace(
                    ItemID="333",
                    ListingDetails=SimpleNamespace(StartTime="2026-03-20T09:00:00Z"),
                ),
                CreatedDate="2026-03-25T09:00:00Z",
                PaidTime="2026-03-25T10:00:00Z",
                ShippedTime="2026-03-26T08:00:00Z",
                TransactionPrice=SimpleNamespace(value="35.00", _currencyID="GBP"),
                QuantityPurchased="1",
            )
        ]
    )
    with patch("ebay.selling.execute_with_retry", return_value=_reply(TransactionArray=txn_array)) as mock:
        result = _run(fetch_seller_transactions(days=30))

    assert len(result["transactions"]) == 1
    t = result["transactions"][0]
    assert t["transaction_id"] == "T1"
    assert t["item_id"] == "333"
    assert t["days_to_sell"] == 5
    assert t["transaction_price"] == "35.00"
    # AP #18: assert ModTimeFrom / ModTimeTo passed
    kwargs_payload = mock.call_args.args[1]
    assert "ModTimeFrom" in kwargs_payload
    assert "ModTimeTo" in kwargs_payload


def test_fetch_seller_transactions_days_out_of_range() -> None:
    with pytest.raises(ValueError, match=r"days must be"):
        _run(fetch_seller_transactions(days=31))


def test_fetch_listing_feedback_happy_path() -> None:
    fb_array = SimpleNamespace(
        FeedbackDetail=SimpleNamespace(
            CommentingUser="buyer1",
            CommentText="Fast delivery",
            CommentTime="2026-04-01T10:00:00Z",
            CommentType="Positive",
            ItemAsDescribed="5",
            CommunicationRating="5",
            ShippingTimeRating="5",
        )
    )
    with patch("ebay.selling.execute_with_retry", return_value=_reply(FeedbackDetailArray=fb_array)) as mock:
        result = _run(fetch_listing_feedback(item_id="999", days=90))

    assert result["item_id"] == "999"
    assert result["feedback_count"] == 1
    assert result["dsr_item_as_described_avg"] == 5.0
    assert result["entries"][0]["comment_type"] == "Positive"
    # AP #18: ItemID passed
    assert mock.call_args.args[1]["ItemID"] == "999"


def test_fetch_listing_feedback_requires_item_id() -> None:
    with pytest.raises(ValueError, match="item_id"):
        _run(fetch_listing_feedback(item_id=""))


def test_fetch_listing_cases_happy_path() -> None:
    cases = SimpleNamespace(
        Case=[
            SimpleNamespace(
                CaseID="CASE-1",
                CaseType="EBP_SNAD",
                CaseStatus="OPEN",
                CreationDate="2026-04-01T10:00:00Z",
                TransactionID="T1",
            ),
            SimpleNamespace(
                CaseID="CASE-2",
                CaseType="EBP_INR",
                CaseStatus="CLOSED",
                CreationDate="2026-03-15T10:00:00Z",
                TransactionID="T2",
            ),
        ]
    )
    with patch("ebay.selling.execute_with_retry", return_value=_reply(CaseArray=cases)) as mock:
        result = _run(fetch_listing_cases(item_id="999"))

    assert result["total_cases"] == 2
    assert result["open_cases"] == 1
    # AP #18: filter + ItemID passed
    payload = mock.call_args.args[1]
    assert payload["ItemID"] == "999"
    assert "CaseTypeFilter" in payload


def test_fetch_listing_cases_requires_item_id() -> None:
    with pytest.raises(ValueError, match="item_id"):
        _run(fetch_listing_cases(item_id=""))
