"""Tests for ebay.best_offers Trading API wrappers (Issue #16 Phase 2).

8 tests covering the two wrappers' payload contracts + parsing tolerance.
All mocks assert `call_args[0][1][...]` (the data-dict the eBay payload field
receives) explicitly per AP #18 — never rely on `**kwargs` swallowing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ebay.best_offers import get_pending_best_offers, respond_to_best_offer


def _run(coro):
    """Repo convention — sync test wraps async via asyncio.run()."""
    return asyncio.run(coro)


def _make_response(reply: object) -> SimpleNamespace:
    """Mimic ebaysdk's response.reply shape."""
    return SimpleNamespace(reply=reply)


# ---------------------------------------------------------------------------
# get_pending_best_offers
# ---------------------------------------------------------------------------


def test_get_pending_best_offers_returns_parsed_list() -> None:
    """Per-seller response with one item + one offer → 1-element list with 8 fields."""
    fake_offer = SimpleNamespace(
        BestOfferID="abc123",
        Buyer=SimpleNamespace(UserID="buyer_uk"),
        Price=SimpleNamespace(value=45.00),
        BuyerMessage="any flexibility on price?",
        ReceivedTime="2026-05-02T14:30:00Z",
        ExpirationTime="2026-05-04T14:30:00Z",
        BestOfferCodeType="ManualBestOffer",
    )
    fake_item = SimpleNamespace(
        ItemID="287260458724",
        BestOfferArray=SimpleNamespace(BestOffer=fake_offer),
    )
    fake_reply = SimpleNamespace(ItemArray=SimpleNamespace(Item=fake_item))

    with patch(
        "ebay.client.execute_with_retry", return_value=_make_response(fake_reply)
    ) as mock_call:
        result = _run(get_pending_best_offers())

    assert len(result) == 1
    assert result[0]["offer_id"] == "abc123"
    assert result[0]["item_id"] == "287260458724"
    assert result[0]["buyer_user_id"] == "buyer_uk"
    assert result[0]["buyer_offer_gbp"] == 45.00
    assert result[0]["buyer_message"] == "any flexibility on price?"
    assert result[0]["offer_timestamp_iso"] == "2026-05-02T14:30:00Z"
    assert result[0]["expiration_iso"] == "2026-05-04T14:30:00Z"
    assert result[0]["best_offer_code_type"] == "ManualBestOffer"
    # Issue #30 AC1.3 — Quantity absent on the fixture → default 1.
    assert result[0]["quantity"] == 1

    # AP #18 — assert eBay payload explicitly
    assert mock_call.call_args[0][0] == "GetBestOffers"
    assert mock_call.call_args[0][1]["BestOfferStatus"] == "Active"


# ---------------------------------------------------------------------------
# Issue #30 AC1.4 — quantity-field extraction tests on _parse_offer_node
# ---------------------------------------------------------------------------


def test_get_pending_best_offers_extracts_explicit_quantity() -> None:
    """`<Quantity>3</Quantity>` on a multi-qty offer → result["quantity"] == 3."""
    fake_offer = SimpleNamespace(
        BestOfferID="qty3",
        Buyer=SimpleNamespace(UserID="bulk_buyer"),
        Price=SimpleNamespace(value=270.00),
        BuyerMessage="3 units please",
        ReceivedTime="2026-05-04T11:34:00Z",
        ExpirationTime="2026-05-06T11:34:00Z",
        BestOfferCodeType="ManualBestOffer",
        Quantity=3,
    )
    fake_item = SimpleNamespace(
        ItemID="000000000001",
        BestOfferArray=SimpleNamespace(BestOffer=fake_offer),
    )
    fake_reply = SimpleNamespace(ItemArray=SimpleNamespace(Item=fake_item))

    with patch("ebay.client.execute_with_retry", return_value=_make_response(fake_reply)):
        result = _run(get_pending_best_offers())

    assert len(result) == 1
    assert result[0]["quantity"] == 3
    assert isinstance(result[0]["quantity"], int)


def test_get_pending_best_offers_defaults_quantity_when_absent() -> None:
    """ebaysdk omits `<Quantity>` for single-qty offers → result["quantity"] == 1."""
    fake_offer = SimpleNamespace(
        BestOfferID="qty_omitted",
        Buyer=SimpleNamespace(UserID="solo_buyer"),
        Price=SimpleNamespace(value=45.00),
        BuyerMessage="",
        ReceivedTime="2026-05-04T09:00:00Z",
        ExpirationTime="2026-05-06T09:00:00Z",
        BestOfferCodeType="ManualBestOffer",
        # Quantity attr deliberately absent
    )
    fake_item = SimpleNamespace(
        ItemID="000000000002",
        BestOfferArray=SimpleNamespace(BestOffer=fake_offer),
    )
    fake_reply = SimpleNamespace(ItemArray=SimpleNamespace(Item=fake_item))

    with patch("ebay.client.execute_with_retry", return_value=_make_response(fake_reply)):
        result = _run(get_pending_best_offers())

    assert result[0]["quantity"] == 1
    assert isinstance(result[0]["quantity"], int)


def test_get_pending_best_offers_coerces_string_quantity_to_int() -> None:
    """ebaysdk sometimes decodes XML scalars as str — defensive int-coerce.

    Mirrors the existing `buyer_offer_gbp = float(...)` defensive pattern in
    `_parse_offer_node`. Confirmed by AC5.1 read-only live probe (see
    docs/research/ebay/11_EBAY_API_AND_MCP_SERVER.md).
    """
    fake_offer = SimpleNamespace(
        BestOfferID="qty_str",
        Buyer=SimpleNamespace(UserID="any_buyer"),
        Price=SimpleNamespace(value=180.00),
        BuyerMessage="",
        ReceivedTime="2026-05-04T10:00:00Z",
        ExpirationTime="2026-05-06T10:00:00Z",
        BestOfferCodeType="ManualBestOffer",
        Quantity="2",  # str-typed scalar from ebaysdk on some response paths
    )
    fake_item = SimpleNamespace(
        ItemID="000000000003",
        BestOfferArray=SimpleNamespace(BestOffer=fake_offer),
    )
    fake_reply = SimpleNamespace(ItemArray=SimpleNamespace(Item=fake_item))

    with patch("ebay.client.execute_with_retry", return_value=_make_response(fake_reply)):
        result = _run(get_pending_best_offers())

    assert result[0]["quantity"] == 2
    assert isinstance(result[0]["quantity"], int)


def test_get_pending_best_offers_handles_unparseable_quantity_safely() -> None:
    """Garbage `<Quantity>` value (non-numeric str / negative / 0) → default 1.

    Belt-and-braces — fail-soft on garbage input rather than raising mid-poll
    and dropping the rest of the items in the iteration. The responder script
    still treats `quantity == 1` as the qty-1 tier.
    """
    fake_offer = SimpleNamespace(
        BestOfferID="qty_garbage",
        Buyer=SimpleNamespace(UserID="any_buyer"),
        Price=SimpleNamespace(value=50.00),
        BuyerMessage="",
        ReceivedTime="2026-05-04T10:00:00Z",
        ExpirationTime="2026-05-06T10:00:00Z",
        BestOfferCodeType="ManualBestOffer",
        Quantity="not-a-number",
    )
    fake_item = SimpleNamespace(
        ItemID="000000000004",
        BestOfferArray=SimpleNamespace(BestOffer=fake_offer),
    )
    fake_reply = SimpleNamespace(ItemArray=SimpleNamespace(Item=fake_item))

    with patch("ebay.client.execute_with_retry", return_value=_make_response(fake_reply)):
        result = _run(get_pending_best_offers())

    assert result[0]["quantity"] == 1


def test_get_pending_best_offers_empty_when_no_offers() -> None:
    """Empty ItemArray → [] (not None — caller iterates without None guard)."""
    fake_reply = SimpleNamespace()  # no ItemArray attr at all
    with patch("ebay.client.execute_with_retry", return_value=_make_response(fake_reply)):
        result = _run(get_pending_best_offers())
    assert result == []


def test_get_pending_best_offers_filters_pending_status() -> None:
    """AP #18 — verb name + BestOfferStatus filter pinned in payload."""
    fake_reply = SimpleNamespace(ItemArray=SimpleNamespace(Item=None))
    with patch(
        "ebay.client.execute_with_retry", return_value=_make_response(fake_reply)
    ) as mock_call:
        _run(get_pending_best_offers())
    assert mock_call.call_args[0][0] == "GetBestOffers"
    payload = mock_call.call_args[0][1]
    assert payload["BestOfferStatus"] == "Active"
    assert payload["DetailLevel"] == "ReturnAll"


# ---------------------------------------------------------------------------
# respond_to_best_offer
# ---------------------------------------------------------------------------


def test_respond_to_best_offer_accept_action_payload_correct() -> None:
    """AP #18 — Accept payload: ItemID + BestOfferID + Action='Accept' (no CounterOfferPrice)."""
    fake_reply = SimpleNamespace(Ack="Success", Errors=None)
    with patch(
        "ebay.client.execute_with_retry", return_value=_make_response(fake_reply)
    ) as mock_call:
        result = _run(
            respond_to_best_offer(item_id="287260458724", offer_id="abc456", action="Accept")
        )

    assert result["success"] is True
    assert result["ebay_response_status"] == "Success"
    assert result["error_message"] is None

    assert mock_call.call_args[0][0] == "RespondToBestOffer"
    payload = mock_call.call_args[0][1]
    assert payload["ItemID"] == "287260458724"
    assert payload["BestOfferID"] == "abc456"
    assert payload["Action"] == "Accept"
    assert "CounterOfferPrice" not in payload


def test_respond_to_best_offer_counter_requires_counter_price() -> None:
    """ValueError when action='Counter' and counter_price_gbp omitted or non-positive."""
    with pytest.raises(ValueError, match="counter_price_gbp"):
        _run(respond_to_best_offer(item_id="287260458724", offer_id="abc456", action="Counter"))
    with pytest.raises(ValueError, match="counter_price_gbp"):
        _run(
            respond_to_best_offer(
                item_id="287260458724",
                offer_id="abc456",
                action="Counter",
                counter_price_gbp=0,
            )
        )


def test_respond_to_best_offer_counter_payload_includes_currency_id() -> None:
    """AP #18 — Counter payload: CounterOfferPrice.value + @currencyID='GBP'."""
    fake_reply = SimpleNamespace(Ack="Success", Errors=None)
    with patch(
        "ebay.client.execute_with_retry", return_value=_make_response(fake_reply)
    ) as mock_call:
        _run(
            respond_to_best_offer(
                item_id="287260458724",
                offer_id="abc456",
                action="Counter",
                counter_price_gbp=49.0,
            )
        )
    payload = mock_call.call_args[0][1]
    assert payload["Action"] == "Counter"
    assert payload["CounterOfferPrice"]["value"] == 49.0
    assert payload["CounterOfferPrice"]["@currencyID"] == "GBP"


def test_respond_to_best_offer_decline_action_payload_correct() -> None:
    """AP #18 — Decline payload: Action='Decline' (no CounterOfferPrice)."""
    fake_reply = SimpleNamespace(Ack="Success", Errors=None)
    with patch(
        "ebay.client.execute_with_retry", return_value=_make_response(fake_reply)
    ) as mock_call:
        result = _run(
            respond_to_best_offer(item_id="287260458724", offer_id="abc789", action="Decline")
        )
    assert result["success"] is True
    payload = mock_call.call_args[0][1]
    assert payload["Action"] == "Decline"
    assert "CounterOfferPrice" not in payload


def test_respond_to_best_offer_propagates_ebay_error_message() -> None:
    """eBay returns Failure ack + Error node → result has error_message + success=False.

    Simulates the BestOffer-disabled-on-listing case (error code 21916) — operator
    toggled BO off after our poll. AC3.5 expects this to be caught + JSONL'd
    by the responder, never cascading to the next offer.
    """
    fake_error = SimpleNamespace(ErrorCode="21916", LongMessage="Best Offer not available")
    fake_reply = SimpleNamespace(Ack="Failure", Errors=fake_error)
    with patch("ebay.client.execute_with_retry", return_value=_make_response(fake_reply)):
        result = _run(
            respond_to_best_offer(item_id="287260458724", offer_id="def123", action="Accept")
        )

    assert result["success"] is False
    assert result["ebay_response_status"] == "Failure"
    assert result["ebay_response_code"] == "21916"
    assert result["error_message"] == "Best Offer not available"
