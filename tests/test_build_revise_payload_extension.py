"""Tests for the Phase 4 build_revise_payload extension (#23 + #24).

Covers:
- picture_urls: payload shape, MAX_PICTURE_URLS cap, joined-chars cap.
- best_offer_enabled: BestOfferDetails.BestOfferEnabled stringified bool.
- best_offer_auto_accept_gbp / best_offer_auto_decline_gbp: ListingDetails
  field placement (D2 verified — NOT BestOfferDetails), Decimal-stringified
  to two dp, currency parameterised.
- _assert_no_quantity invariant preserved across the new params.
"""

from __future__ import annotations

import pytest

from ebay.listings import MAX_PICTURE_URLS, MAX_PICTURE_URLS_JOINED_CHARS, build_revise_payload


def test_picture_urls_writes_picture_details_block() -> None:
    urls = ["https://i.ebayimg.com/a.jpg", "https://i.ebayimg.com/b.jpg"]
    payload = build_revise_payload(item_id="111", picture_urls=urls)
    assert payload["Item"]["PictureDetails"] == {"PictureURL": urls}


def test_picture_urls_omitted_when_none() -> None:
    payload = build_revise_payload(item_id="111", title="x")
    assert "PictureDetails" not in payload["Item"]


def test_picture_urls_rejects_over_24() -> None:
    too_many = [f"https://i.ebayimg.com/{i}.jpg" for i in range(MAX_PICTURE_URLS + 1)]
    with pytest.raises(ValueError, match=r"at most 24 URLs"):
        build_revise_payload(item_id="111", picture_urls=too_many)


def test_picture_urls_rejects_over_3975_joined_chars() -> None:
    big = "https://i.ebayimg.com/" + "x" * 200 + ".jpg"
    urls = [big] * 20
    assert sum(len(u) for u in urls) >= MAX_PICTURE_URLS_JOINED_CHARS
    with pytest.raises(ValueError, match=r"total length \d+ chars exceeds"):
        build_revise_payload(item_id="111", picture_urls=urls)


def test_best_offer_enabled_true_stringified() -> None:
    payload = build_revise_payload(item_id="111", best_offer_enabled=True)
    assert payload["Item"]["BestOfferDetails"] == {"BestOfferEnabled": "true"}


def test_best_offer_enabled_false_stringified() -> None:
    payload = build_revise_payload(item_id="111", best_offer_enabled=False)
    assert payload["Item"]["BestOfferDetails"] == {"BestOfferEnabled": "false"}


def test_best_offer_enabled_none_omits_block() -> None:
    payload = build_revise_payload(item_id="111", title="x")
    assert "BestOfferDetails" not in payload["Item"]


def test_best_offer_auto_accept_in_listing_details_not_best_offer_details() -> None:
    """D2 verified — BestOfferAutoAcceptPrice lives under ListingDetails."""
    payload = build_revise_payload(
        item_id="111",
        best_offer_enabled=True,
        best_offer_auto_accept_gbp=44.0,
    )
    listing_details = payload["Item"]["ListingDetails"]
    assert listing_details["BestOfferAutoAcceptPrice"] == {
        "#text": "44.00",
        "@attrs": {"currencyID": "GBP"},
    }
    # NOT placed under BestOfferDetails — keep them separate.
    best_offer_details = payload["Item"]["BestOfferDetails"]
    assert "BestOfferAutoAcceptPrice" not in best_offer_details
    assert "MinimumBestOfferPrice" not in best_offer_details


def test_minimum_best_offer_price_in_listing_details() -> None:
    """D2 verified — MinimumBestOfferPrice (auto-decline floor) lives under ListingDetails."""
    payload = build_revise_payload(
        item_id="111",
        best_offer_auto_decline_gbp=36.0,
    )
    assert payload["Item"]["ListingDetails"]["MinimumBestOfferPrice"] == {
        "#text": "36.00",
        "@attrs": {"currencyID": "GBP"},
    }


def test_both_best_offer_thresholds_set_together() -> None:
    payload = build_revise_payload(
        item_id="111",
        best_offer_enabled=True,
        best_offer_auto_accept_gbp=44.0,
        best_offer_auto_decline_gbp=36.0,
    )
    listing_details = payload["Item"]["ListingDetails"]
    assert listing_details["BestOfferAutoAcceptPrice"]["#text"] == "44.00"
    assert listing_details["MinimumBestOfferPrice"]["#text"] == "36.00"


def test_best_offer_amounts_decimal_no_float_drift() -> None:
    """Decimal(str(...)) avoids float drift like Decimal(0.1)=0.1000000000000000055..."""
    payload = build_revise_payload(item_id="111", best_offer_auto_accept_gbp=0.1)
    assert payload["Item"]["ListingDetails"]["BestOfferAutoAcceptPrice"]["#text"] == "0.10"


def test_best_offer_amount_two_dp_rounds_correctly() -> None:
    payload = build_revise_payload(item_id="111", best_offer_auto_accept_gbp=44.567)
    # ROUND_HALF_EVEN — 44.567 -> 44.57
    assert payload["Item"]["ListingDetails"]["BestOfferAutoAcceptPrice"]["#text"] == "44.57"


def test_best_offer_currency_override() -> None:
    payload = build_revise_payload(
        item_id="111",
        best_offer_auto_accept_gbp=44.0,
        currency="USD",
    )
    assert (
        payload["Item"]["ListingDetails"]["BestOfferAutoAcceptPrice"]["@attrs"]["currencyID"]
        == "USD"
    )


def test_assert_no_quantity_still_fires_on_revise_path() -> None:
    """The _assert_no_quantity safety invariant must still fire when picture_urls /
    best_offer params are added — those code paths must not introduce a Quantity key."""
    payload = build_revise_payload(
        item_id="111",
        picture_urls=["https://i.ebayimg.com/a.jpg"],
        best_offer_enabled=True,
        best_offer_auto_accept_gbp=44.0,
        best_offer_auto_decline_gbp=36.0,
    )
    assert "Quantity" not in payload["Item"]
    assert "Quantity" not in payload["Item"].get("ListingDetails", {})
    assert "Quantity" not in payload["Item"].get("BestOfferDetails", {})
    assert "Quantity" not in payload["Item"].get("PictureDetails", {})


def test_picture_urls_at_exactly_24_accepted() -> None:
    urls = [f"https://i.ebayimg.com/{i:03d}.jpg" for i in range(MAX_PICTURE_URLS)]
    payload = build_revise_payload(item_id="111", picture_urls=urls)
    assert len(payload["Item"]["PictureDetails"]["PictureURL"]) == MAX_PICTURE_URLS


def test_listing_details_merges_with_other_listing_detail_fields() -> None:
    """If both auto_accept and auto_decline are set, both keys land in same ListingDetails."""
    payload = build_revise_payload(
        item_id="111",
        best_offer_auto_accept_gbp=44.0,
        best_offer_auto_decline_gbp=36.0,
    )
    listing_details = payload["Item"]["ListingDetails"]
    assert "BestOfferAutoAcceptPrice" in listing_details
    assert "MinimumBestOfferPrice" in listing_details
