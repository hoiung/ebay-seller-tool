"""Tests for the Phase 1.1 extension of listing_to_dict."""

from types import SimpleNamespace

from ebay.listings import listing_to_dict


def _build_item(**overrides):
    base = SimpleNamespace(
        ItemID="12345",
        Title="Seagate Enterprise 2TB",
        SellingStatus=SimpleNamespace(
            CurrentPrice=SimpleNamespace(value="35.00", _currencyID="GBP"),
            QuantitySold="3",
        ),
        Quantity="4",
        QuantityAvailable="1",
        ListingDetails=SimpleNamespace(
            ViewItemURL="https://www.ebay.co.uk/itm/12345",
            StartTime="2026-03-15T10:22:00Z",
            EndTime="2026-04-14T10:22:00Z",
            RelistCount="0",
        ),
        BestOfferCount="1",
        BestOfferDetails=SimpleNamespace(
            BestOfferEnabled="true",
            BestOfferCount="1",
            NewBestOffer="false",
        ),
        QuestionCount="2",
        WatchCount="7",
        HitCount="142",
        ShippingDetails=SimpleNamespace(
            ShippingType="Flat",
            ShippingServiceOptions=SimpleNamespace(
                ShippingService="Evri Standard",
                ShippingServiceCost=SimpleNamespace(value="0.00"),
                FreeShipping="true",
            ),
        ),
        ReturnPolicy=SimpleNamespace(
            ReturnsAcceptedOption="ReturnsAccepted",
            ReturnsWithinOption="Days_30",
            ShippingCostPaidByOption="Seller",
        ),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_listing_to_dict_surfaces_new_fields() -> None:
    d = listing_to_dict(_build_item())
    assert d["quantity_sold"] == 3
    assert d["best_offer_count"] == 1
    assert d["best_offer_enabled"] is True
    assert d["question_count"] == 2
    assert d["relist_count"] == 0
    assert d["start_time"].startswith("2026-03-15")
    assert d["end_time"].startswith("2026-04-14")
    assert d["days_on_site"] is not None
    assert d["shipping"]["service"] == "Evri Standard"
    assert d["shipping"]["free"] is True
    assert d["return_policy"]["returns_accepted"] is True
    assert d["return_policy"]["period_days"] == 30
    assert d["return_policy"]["buyer_pays"] is False
    # Phase 1.4 — promoted_listing absent on default fixture → False
    assert d["promoted_listing"] is False


def test_listing_to_dict_promoted_listing_true() -> None:
    """Phase 1.4.2 — PromotedListing=true surfaces as True."""
    item = _build_item(
        ListingDetails=SimpleNamespace(
            ViewItemURL="https://www.ebay.co.uk/itm/12345",
            StartTime="2026-03-15T10:22:00Z",
            EndTime="2026-04-14T10:22:00Z",
            RelistCount="0",
            PromotedListing="true",
        )
    )
    d = listing_to_dict(item)
    assert d["promoted_listing"] is True


def test_listing_to_dict_promoted_listing_false_default() -> None:
    """Phase 1.4.2 — PromotedListing absent or 'false' → False."""
    item = _build_item(
        ListingDetails=SimpleNamespace(
            ViewItemURL="https://www.ebay.co.uk/itm/12345",
            StartTime="2026-03-15T10:22:00Z",
            EndTime="2026-04-14T10:22:00Z",
            RelistCount="0",
            PromotedListing="false",
        )
    )
    d = listing_to_dict(item)
    assert d["promoted_listing"] is False


def test_listing_to_dict_handles_missing_optional_fields() -> None:
    # #16 fix: BestOfferEnabled defaults to False (boolean-only contract) when
    # the element is absent from GetItem response — never None. Real-shape: the
    # field lives under BestOfferDetails (not on Item root).
    item = _build_item(BestOfferDetails=None, QuestionCount=None, BestOfferCount=None)
    d = listing_to_dict(item)
    assert d["best_offer_count"] == 0
    assert d["question_count"] == 0
    assert d["best_offer_enabled"] is False


def test_listing_to_dict_best_offer_enabled_absent() -> None:
    # Regression: simulates GetItem response with the BestOfferDetails element
    # absent entirely (eBay omits the parent block for listings without Best
    # Offer configured). Bug-shaped path before ebay-ops#17 fix: parser read
    # `getattr(item, "BestOfferEnabled")` directly off the Item root, which
    # silently returned absent and produced 0/N false-negatives even when
    # listings WERE Best Offer enabled.
    item = _build_item(BestOfferDetails=None)
    d = listing_to_dict(item)
    assert d["best_offer_enabled"] is False  # NOT None, NOT True
    assert isinstance(d["best_offer_enabled"], bool)


def test_listing_to_dict_best_offer_enabled_true_when_nested() -> None:
    # Regression: confirms the parser reads from BestOfferDetails.BestOfferEnabled
    # (the real eBay API shape per GetItem) — NOT from Item.BestOfferEnabled
    # which never exists. Pre-fix: this test would have failed because the
    # parser ignored the nested field entirely.
    from types import SimpleNamespace

    item = _build_item(
        BestOfferDetails=SimpleNamespace(
            BestOfferEnabled="true",
            BestOfferCount="0",
            NewBestOffer="false",
        ),
    )
    d = listing_to_dict(item)
    assert d["best_offer_enabled"] is True
    assert isinstance(d["best_offer_enabled"], bool)


def test_listing_to_dict_best_offer_enabled_false_string() -> None:
    # New regression fixture: BestOfferDetails.BestOfferEnabled="false" XML
    # string value (real-shape: nested under BestOfferDetails per ebay-ops#17 fix).
    from types import SimpleNamespace

    item = _build_item(
        BestOfferDetails=SimpleNamespace(
            BestOfferEnabled="false",
            BestOfferCount="0",
            NewBestOffer="false",
        ),
    )
    d = listing_to_dict(item)
    assert d["best_offer_enabled"] is False
    assert isinstance(d["best_offer_enabled"], bool)


def test_listing_to_dict_surfaces_best_offer_thresholds() -> None:
    """AP #18 surfaced gap — Item.ListingDetails.BestOfferAutoAcceptPrice +
    MinimumBestOfferPrice need to be reachable for restore round-trips and
    for recommend_best_offer_thresholds to compare current vs proposed."""
    item = _build_item(
        ListingDetails=SimpleNamespace(
            ViewItemURL="https://www.ebay.co.uk/itm/12345",
            StartTime="2026-03-15T10:22:00Z",
            EndTime="2026-04-14T10:22:00Z",
            RelistCount="0",
            BestOfferAutoAcceptPrice=SimpleNamespace(value="44.00", _currencyID="GBP"),
            MinimumBestOfferPrice=SimpleNamespace(value="36.00", _currencyID="GBP"),
        )
    )
    d = listing_to_dict(item)
    assert d["best_offer_auto_accept_gbp"] == 44.0
    assert d["best_offer_auto_decline_gbp"] == 36.0


def test_listing_to_dict_best_offer_thresholds_absent_returns_none() -> None:
    """When BestOfferAutoAcceptPrice / MinimumBestOfferPrice elements absent,
    surface as None (not 0, not 'missing') — matches eBay's 'Best Offer not
    configured' state."""
    d = listing_to_dict(_build_item())  # default fixture has no thresholds
    assert d["best_offer_auto_accept_gbp"] is None
    assert d["best_offer_auto_decline_gbp"] is None


def test_listing_to_dict_handles_missing_shipping() -> None:
    item = _build_item(ShippingDetails=None)
    d = listing_to_dict(item)
    assert d["shipping"] is None


def test_listing_to_dict_handles_missing_return_policy() -> None:
    item = _build_item(ReturnPolicy=None)
    d = listing_to_dict(item)
    assert d["return_policy"] is None


def test_listing_to_dict_business_policies_response_buyer_pays() -> None:
    """Issue #29 — fetch-side parser surfaces post-Business-Policies state.

    When a listing is enrolled via SellerProfiles → 30d-buyer-pays return profile,
    eBay's GetItem response carries the resolved policy fields (the Profile ID
    is server-side-resolved before the response is rendered). Asserts the parser
    correctly reports returns_accepted=True / period_days=30 / buyer_pays=True
    — the canonical post-#29 store state. This complements the operator's
    end-to-end verification (22/22 listings in /tmp/ebay-listings-live.json
    showed exactly this policy after the bulk migration).
    """
    rp = SimpleNamespace(
        ReturnsAcceptedOption="ReturnsAccepted",
        ReturnsWithinOption="Days_30",
        ShippingCostPaidByOption="Buyer",
    )
    item = _build_item(ReturnPolicy=rp)
    d = listing_to_dict(item)
    assert d["return_policy"]["returns_accepted"] is True
    assert d["return_policy"]["period_days"] == 30
    assert d["return_policy"]["buyer_pays"] is True


def test_days_on_site_reasonable() -> None:
    d = listing_to_dict(_build_item())
    # From 2026-03-15 to "now" (test runs after that date)
    assert d["days_on_site"] is None or d["days_on_site"] >= 0


def test_listing_to_dict_quantity_sold_zero_when_missing() -> None:
    selling_status = SimpleNamespace(
        CurrentPrice=SimpleNamespace(value="35.00", _currencyID="GBP"),
        QuantitySold=None,
    )
    item = _build_item(SellingStatus=selling_status)
    d = listing_to_dict(item)
    assert d["quantity_sold"] == 0
