"""Unit tests for ebay.browse (Issue #4 Phase 3)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ebay import browse, oauth


def setup_function() -> None:
    oauth.reset_token_cache()


def _run(coro):
    return asyncio.run(coro)


def _fake_browse_client(payload: dict) -> MagicMock:
    client = MagicMock()
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    resp.text = "{}"
    resp.json.return_value = payload
    client.get.return_value = resp
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def test_competitor_prices_requires_part_number() -> None:
    with pytest.raises(ValueError, match="part_number"):
        _run(browse.fetch_competitor_prices(part_number=""))


def test_competitor_prices_invalid_condition() -> None:
    with pytest.raises(ValueError, match="Unknown condition"):
        _run(browse.fetch_competitor_prices(part_number="ST2000NM", condition="BOGUS"))


def test_competitor_prices_excludes_own_seller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBAY_OWN_SELLER_USERNAME", "myownshop")
    fake = _fake_browse_client(
        {
            "itemSummaries": [
                {
                    "itemId": "1",
                    "title": "ST2000NM 2TB",
                    "price": {"value": "30.00", "currency": "GBP"},
                    "seller": {"username": "myownshop"},
                    "condition": "Used",
                },
                {
                    "itemId": "2",
                    "title": "ST2000NM 2TB",
                    "price": {"value": "40.00", "currency": "GBP"},
                    "seller": {"username": "othershop"},
                    "condition": "Used",
                },
            ]
        }
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(part_number="ST2000NM", condition="USED", limit=50)
        )
    assert result["count"] == 1
    assert result["listings"][0]["seller"] == "othershop"
    # AP #18: verify the filter + query propagated correctly to the HTTP call
    call = fake.get.call_args
    assert call.args[0] == "/buy/browse/v1/item_summary/search"
    params = call.kwargs.get("params") or call.args[1]
    assert params["q"] == "ST2000NM"
    # Issue #14 Phase 2.4 — single-ID per Browse call (pipe-separator silently
    # truncated by eBay per live curl verification 2026-04-25).
    assert "conditionIds:{3000}" in params["filter"]
    assert "itemLocationCountry:{GB}" in params["filter"]
    assert params["limit"] == "50"


def test_competitor_prices_null_seller(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-6.3: Browse API response with seller=null must not raise.

    Observed edge case — some scraped / cached listings omit the seller
    object entirely. ebay/browse.py:76 falls back via `(item.get("seller")
    or {}).get("username", "")` which must yield empty string, and the
    own-seller filter must pass it through.
    """
    monkeypatch.setenv("EBAY_OWN_SELLER_USERNAME", "myownshop")
    fake = _fake_browse_client(
        {
            "itemSummaries": [
                {
                    "seller": None,  # explicit null
                    "price": {"value": "25.00", "currency": "GBP"},
                    "title": "Listing with null seller",
                },
                {
                    # seller key entirely absent
                    "price": {"value": "30.00", "currency": "GBP"},
                    "title": "Listing with no seller key",
                },
            ]
        }
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(browse.fetch_competitor_prices(part_number="PN", condition="USED"))
    # Both null-seller items must be retained (not filtered, not crashed).
    assert result["count"] == 2
    assert result["min"] == 25.0
    assert result["max"] == 30.0


def test_competitor_prices_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBAY_OWN_SELLER_USERNAME", "myownshop")
    fake = _fake_browse_client({"itemSummaries": []})
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(browse.fetch_competitor_prices(part_number="NONEXISTENT"))
    # Fail-fast: count=0 explicit, no silent-defaulted prices
    assert result["count"] == 0
    assert result["min"] is None
    assert result["median"] is None


def test_competitor_prices_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    items = [
        {
            "itemId": str(i),
            "title": "foo",
            "price": {"value": str(price), "currency": "GBP"},
            "seller": {"username": f"seller{i}"},
            "condition": "Used",
            "shippingOptions": [{"shippingCost": {"value": "0.00"}}],
            "bestOfferEnabled": i % 2 == 0,
        }
        for i, price in enumerate([10, 20, 30, 40, 50])
    ]
    fake = _fake_browse_client({"itemSummaries": items})
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(browse.fetch_competitor_prices(part_number="PN"))
    assert result["count"] == 5
    assert result["min"] == 10.0
    assert result["max"] == 50.0
    assert result["median"] == 30.0
    assert result["shipping_free_pct"] == 100.0


def test_competitor_prices_extension_fields_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 1.1 + 1.2 — itemCreationDate + 7 new Browse fields surfaced.

    Mocked Browse response includes every defensive-lookup field; assert
    each appears in the per-listing dict with the correct value.
    """
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    fake = _fake_browse_client(
        {
            "itemSummaries": [
                {
                    "itemId": "v1|EXT",
                    "title": "Test listing extension fields",
                    "price": {"value": "40.00", "currency": "GBP"},
                    "seller": {
                        "username": "ext-seller",
                        "feedbackPercentage": "99.5",
                        "feedbackScore": 12345,
                    },
                    "condition": "Used",
                    "itemWebUrl": "https://ebay.co.uk/itm/EXT",
                    "itemCreationDate": "2026-01-15T10:30:00.000Z",
                    "image": {"imageUrl": "https://i.ebayimg.com/EXT.jpg"},
                    "additionalImages": [
                        {"imageUrl": "https://i.ebayimg.com/EXT_2.jpg"},
                        {"imageUrl": "https://i.ebayimg.com/EXT_3.jpg"},
                    ],
                    "topRatedBuyingExperience": True,
                    "returnTerms": {"returnsAccepted": True, "returnsWithinDays": 30},
                }
            ]
        }
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(browse.fetch_competitor_prices(part_number="EXT"))

    assert result["count"] == 1
    listing = result["listings"][0]
    assert listing["item_creation_date"] == "2026-01-15T10:30:00.000Z"
    assert listing["image_url"] == "https://i.ebayimg.com/EXT.jpg"
    assert listing["additional_image_count"] == 2
    assert listing["seller_feedback_pct"] == "99.5"
    assert listing["seller_feedback_score"] == 12345
    assert listing["top_rated"] is True
    assert listing["returns_accepted"] is True
    assert listing["returns_within_days"] == 30
    # Phase 7 plumbing fix — best_offer_enabled is per-listing (not just aggregate %).
    assert listing["best_offer_enabled"] is False  # not set in mock → defaults False


def test_competitor_prices_extension_fields_missing_default_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 1.2.2 — every extension field defaults to None (or 0 for image count) when absent."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    fake = _fake_browse_client(
        {
            "itemSummaries": [
                {
                    # Bare-minimum item: no creation date, no image obj, no return terms,
                    # no additional images, no top-rated flag, sparse seller obj.
                    "itemId": "v1|MIN",
                    "title": "Minimal listing",
                    "price": {"value": "25.00", "currency": "GBP"},
                    "seller": {"username": "min-seller"},
                    "condition": "Used",
                }
            ]
        }
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(browse.fetch_competitor_prices(part_number="MIN"))

    listing = result["listings"][0]
    assert listing["item_creation_date"] is None
    assert listing["image_url"] is None
    assert listing["additional_image_count"] == 0
    assert listing["seller_feedback_pct"] is None
    assert listing["seller_feedback_score"] is None
    assert listing["top_rated"] is None
    assert listing["returns_accepted"] is None
    assert listing["returns_within_days"] is None


def test_competitor_prices_with_own_listing_surfaces_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #14 Phase 5.1 — fetch_competitor_prices(own_listing=...) runs the 3-layer pipeline.

    Asserts that when own_listing context is provided, the result dict gains
    `audit` (flat 6-key) and `audit_verbose`, and listings/percentiles reflect
    the kept comps (low-quality drops + scoring + outlier all filter the pool).
    """
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    items = [
        # Clean apple-to-apples comp (kept).
        {
            "itemId": "v1|good",
            "title": "ST2000NX0253 2.5 SAS HDD",
            "price": {"value": "30.00", "currency": "GBP"},
            "seller": {"username": "a", "feedbackPercentage": "99.5", "feedbackScore": 1000},
            "condition": "Used",
            "conditionId": "3000",
            "image": {"imageUrl": "https://i.ebayimg.com/g.jpg"},
            "additionalImages": [{"imageUrl": "https://i.ebayimg.com/g2.jpg"}],
            "returnTerms": {"returnsAccepted": True, "returnsWithinDays": 30},
            "topRatedBuyingExperience": True,
        },
        # Bundle reject (Layer-1 hard reject).
        {
            "itemId": "v1|bundle",
            "title": "Lot of 10 ST2000NX0253 drives",
            "price": {"value": "150.00", "currency": "GBP"},
            "seller": {"username": "b", "feedbackPercentage": "99.0", "feedbackScore": 500},
            "condition": "Used",
            "conditionId": "3000",
            "image": {"imageUrl": "https://i.ebayimg.com/b.jpg"},
            "returnTerms": {"returnsAccepted": True, "returnsWithinDays": 30},
        },
        # Broken-or-parts reject.
        {
            "itemId": "v1|parts",
            "title": "ST2000NX0253 for parts",
            "price": {"value": "5.00", "currency": "GBP"},
            "seller": {"username": "c", "feedbackPercentage": "99.0", "feedbackScore": 500},
            "condition": "Used",
            "conditionId": "3000",
            "image": {"imageUrl": "https://i.ebayimg.com/p.jpg"},
            "returnTerms": {"returnsAccepted": True, "returnsWithinDays": 30},
        },
    ]
    own = {
        "title": "ST2000NX0253 2.5 SAS HDD",
        "specifics": {"MPN": ["ST2000NX0253"], "Form Factor": ['2.5"']},
        "condition_id": "3000",
        "condition_name": "Used",
    }
    fake = _fake_browse_client({"itemSummaries": items})
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(
                part_number="ST2000NX0253",
                condition="USED",
                own_listing=own,
                own_live_price=35.0,
            )
        )

    assert "audit" in result
    assert set(result["audit"].keys()) == {
        "raw_count",
        "kept",
        "dropped_low_quality",
        "dropped_apple_to_apples",
        "dropped_stale",
        "dropped_outlier",
    }
    assert result["audit"]["raw_count"] == 3
    assert result["audit"]["dropped_low_quality"] == 2  # bundle + parts
    assert result["audit"]["kept"] == 1  # only the clean comp
    assert result["count"] == 1  # percentiles re-computed from kept pool
    assert result["min"] == 30.0
    assert "audit_verbose" in result
    assert result["audit_verbose"]["low_quality_drops"]["bundle"] == 1
    assert result["audit_verbose"]["low_quality_drops"]["broken_or_parts"] == 1


def test_competitor_prices_without_own_listing_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: own_listing=None → raw distribution shape, no audit key."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    fake = _fake_browse_client(
        {
            "itemSummaries": [
                {
                    "itemId": "1",
                    "title": "ST2000 2.5 SAS",
                    "price": {"value": "30.00", "currency": "GBP"},
                    "seller": {"username": "a"},
                    "condition": "Used",
                }
            ]
        }
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(browse.fetch_competitor_prices(part_number="ST2000"))
    assert "audit" not in result
    assert "audit_verbose" not in result
    assert result["count"] == 1


def test_competitor_prices_mixed_currency_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug 0.1 — multi-currency Browse response must raise.

    Filter `itemLocationCountry` should prevent this in production, but
    if eBay ever returns mixed currencies the aggregate min/median/max are
    meaningless. Raise loudly rather than silently latching the first one.
    """
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    fake = _fake_browse_client(
        {
            "itemSummaries": [
                {
                    "itemId": "1",
                    "title": "GBP item",
                    "price": {"value": "30.00", "currency": "GBP"},
                    "seller": {"username": "a"},
                    "condition": "Used",
                },
                {
                    "itemId": "2",
                    "title": "EUR item",
                    "price": {"value": "32.00", "currency": "EUR"},
                    "seller": {"username": "b"},
                    "condition": "Used",
                },
            ]
        }
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        with pytest.raises(ValueError, match=r"mixed currencies.*EUR.*GBP"):
            _run(browse.fetch_competitor_prices(part_number="PN", condition="USED"))


def _run_distribution(prices_in: list[float], monkeypatch: pytest.MonkeyPatch) -> dict:
    """Helper for small-N distribution tests."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    items = [
        {
            "itemId": str(i),
            "title": "foo",
            "price": {"value": str(price), "currency": "GBP"},
            "seller": {"username": f"s{i}"},
            "condition": "Used",
        }
        for i, price in enumerate(prices_in)
    ]
    fake = _fake_browse_client({"itemSummaries": items})
    with patch("ebay.browse.get_browse_session", return_value=fake):
        return _run(browse.fetch_competitor_prices(part_number="PN", condition="USED"))


@pytest.mark.parametrize(
    ("prices_in", "expected_min", "expected_p25", "expected_p75", "expected_max"),
    [
        # Bug 0.2 — small-N percentile coverage. Issue spec:
        # N=2: p25==min, p75==max
        # N=3: p25==sorted[0], p75==sorted[2]
        # N=4: p25==sorted[1], p75==sorted[3]
        # N=5: p25==sorted[1], p75==sorted[3]
        ([10.0, 20.0], 10.0, 10.0, 20.0, 20.0),
        ([10.0, 20.0, 30.0], 10.0, 10.0, 30.0, 30.0),
        ([10.0, 20.0, 30.0, 40.0], 10.0, 20.0, 40.0, 40.0),
        ([10.0, 20.0, 30.0, 40.0, 50.0], 10.0, 20.0, 40.0, 50.0),
    ],
)
def test_competitor_prices_small_n_percentiles(
    monkeypatch: pytest.MonkeyPatch,
    prices_in: list[float],
    expected_min: float,
    expected_p25: float,
    expected_p75: float,
    expected_max: float,
) -> None:
    """Bug 0.2 — verify p25/p75 for N=2..5 match issue-spec ranks."""
    result = _run_distribution(prices_in, monkeypatch)
    assert result["count"] == len(prices_in)
    assert result["min"] == expected_min
    assert result["p25"] == expected_p25
    assert result["p75"] == expected_p75
    assert result["max"] == expected_max
