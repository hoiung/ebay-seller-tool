"""Unit tests for ebay.browse (Issue #4 Phase 3 + Issue #444 Part B equivalence loop)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ebay import browse, oauth


def setup_function() -> None:
    oauth.reset_token_cache()
    # Issue #444 Part B — reset filter cache so equivalence-class lookups
    # see fresh YAML each test (some tests below override EBAY_FILTER_CONFIG).
    browse.reset_filter_cache()


def teardown_function() -> None:
    """Reset the filter cache AFTER each test too — prevents tmp_path YAMLs from
    test_browse.py leaking into the next module's lru_cache (test_comp_filter et al).
    monkeypatch reverts EBAY_FILTER_CONFIG but the cached _load_filter_config result
    persists until reset_filter_cache() is called.
    """
    browse.reset_filter_cache()


def _run(coro):
    return asyncio.run(coro)


def _fake_response(payload: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    resp.text = "{}" if status_code == 200 else "ebay error"
    resp.json.return_value = payload
    return resp


def _fake_browse_client(payload: dict) -> MagicMock:
    client = MagicMock()
    resp = _fake_response(payload)
    client.get.return_value = resp
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def _fake_browse_client_seq(payloads: list[dict]) -> MagicMock:
    """Multi-call mock — Issue #444 Part B equivalence-class loop.

    `client.get(...)` returns ``_fake_response(payloads[0])``, ``payloads[1]``, ...
    in order. Use when the orchestrator dispatches >1 Browse call (USED + OPENED
    equivalence classes have N=2).
    """
    client = MagicMock()
    client.get.side_effect = [_fake_response(p) for p in payloads]
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
    """B2.9 (#444) — multi-call mock: USED equivalence class = 3000 + 2750.

    Single-ID-per-call still holds at the API layer (pipe-separator silently
    truncated by eBay per live curl 2026-04-25); orchestrator dispatches one
    call per equivalence-class member.
    """
    monkeypatch.setenv("EBAY_OWN_SELLER_USERNAME", "myownshop")
    payload_3000 = {
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
    payload_2750 = {"itemSummaries": []}
    fake = _fake_browse_client_seq([payload_3000, payload_2750])
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(part_number="ST2000NM", condition="USED", limit=50)
        )
    assert result["count"] == 1
    assert result["listings"][0]["seller"] == "othershop"
    # AP #18 — verify BOTH calls propagated the filter correctly + ordered as 3000 then 2750.
    assert fake.get.call_count == 2
    call_3000, call_2750 = fake.get.call_args_list
    assert call_3000.args[0] == "/buy/browse/v1/item_summary/search"
    params_3000 = call_3000.kwargs.get("params") or call_3000.args[1]
    params_2750 = call_2750.kwargs.get("params") or call_2750.args[1]
    assert params_3000["q"] == "ST2000NM"
    assert "conditionIds:{3000}" in params_3000["filter"]
    assert "itemLocationCountry:{GB}" in params_3000["filter"]
    assert params_3000["limit"] == "50"
    assert "conditionIds:{2750}" in params_2750["filter"]


def test_competitor_prices_null_seller(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-6.3: Browse API response with seller=null must not raise.

    Observed edge case — some scraped / cached listings omit the seller
    object entirely. ebay/browse.py:76 falls back via `(item.get("seller")
    or {}).get("username", "")` which must yield empty string, and the
    own-seller filter must pass it through.
    """
    monkeypatch.setenv("EBAY_OWN_SELLER_USERNAME", "myownshop")
    # Issue #444 Part B — itemIds added so the orchestrator's dedupe (skip None)
    # doesn't drop them; this test cares about null-seller handling, not None-itemId.
    fake = _fake_browse_client(
        {
            "itemSummaries": [
                {
                    "itemId": "ns1",
                    "seller": None,  # explicit null
                    "price": {"value": "25.00", "currency": "GBP"},
                    "title": "Listing with null seller",
                },
                {
                    "itemId": "ns2",
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
                    # Issue #14 AC 2.5.1 — numeric conditionId surfaced per-listing.
                    "conditionId": "3000",
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
    # Issue #14 AC 2.5.1 — condition_id captured from raw conditionId field as string.
    assert listing["condition_id"] == "3000"
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


# ---------------------------------------------------------------------------
# Issue #444 Part B — equivalence-class loop tests (B2.1 – B2.11)
# ---------------------------------------------------------------------------


def _own_listing_min() -> dict:
    """Minimal own_listing for pipeline-mode tests that don't care about scoring."""
    return {
        "title": "Test ST2000",
        "specifics": {"MPN": ["ST2000"], "Form Factor": ['2.5"']},
        "condition_id": "3000",
        "condition_name": "Used",
    }


def _comp_item(item_id: str | None, price: float, condition_id: str = "3000") -> dict:
    """Make a Browse-API-shaped competitor item with an explicit conditionId."""
    return {
        "itemId": item_id,
        "title": f"ST2000 comp {item_id}",
        "price": {"value": str(price), "currency": "GBP"},
        "seller": {
            "username": f"seller-{item_id}",
            "feedbackPercentage": "99.5",
            "feedbackScore": 1000,
        },
        "condition": "Used" if condition_id == "3000" else "Used - Like New",
        "conditionId": condition_id,
        "image": {"imageUrl": "https://i.ebayimg.com/i.jpg"},
        "additionalImages": [{"imageUrl": "https://i.ebayimg.com/i2.jpg"}],
        "returnTerms": {"returnsAccepted": True, "returnsWithinDays": 30},
        "topRatedBuyingExperience": True,
    }


def test_competitor_prices_used_fetches_equivalence_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2.1 — USED triggers two Browse calls (3000 + 2750), merges, per-condition raw counts."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    payload_3000 = {
        "itemSummaries": [_comp_item(str(i), 30.0 + i, "3000") for i in range(1, 6)]
    }
    payload_2750 = {
        "itemSummaries": [_comp_item(f"x{j}", 25.0 + j, "2750") for j in range(1, 4)]
    }
    fake = _fake_browse_client_seq([payload_3000, payload_2750])
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(
                part_number="ST2000",
                condition="USED",
                own_listing=_own_listing_min(),
                own_live_price=35.0,
            )
        )
    # Two Browse calls dispatched in order: 3000 first, 2750 second
    assert fake.get.call_count == 2
    call_3000, call_2750 = fake.get.call_args_list
    params_3000 = call_3000.kwargs.get("params") or call_3000.args[1]
    params_2750 = call_2750.kwargs.get("params") or call_2750.args[1]
    assert "conditionIds:{3000}" in params_3000["filter"]
    assert "conditionIds:{2750}" in params_2750["filter"]
    # Per-condition raw counts surfaced in audit_verbose
    assert result["audit_verbose"]["raw_count_per_condition_id"] == {"3000": 5, "2750": 3}
    # Pre-pipeline merge: 8 unique listings (no item_id overlap between calls)
    assert result["audit"]["raw_count"] == 8


def test_competitor_prices_dedupe_across_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2.2 — same item_id in BOTH calls kept once + None-itemId entry skipped.

    Cond 3000 returns A, B, C + a synthetic None-itemId entry (4 raw).
    Cond 2750 returns C, D, E (3 raw, with C overlapping cond 3000).
    Post-dedupe merged listings = 5 unique (A, B, C, D, E); raw counts BEFORE
    dedupe surfaced as {"3000": 4, "2750": 3} (None-itemId entry counts in raw).
    """
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    cond_3000_items = [
        _comp_item("A", 30.0, "3000"),
        _comp_item("B", 32.0, "3000"),
        _comp_item("C", 34.0, "3000"),
        _comp_item(None, 36.0, "3000"),  # None-itemId — skipped on dedupe
    ]
    cond_2750_items = [
        _comp_item("C", 28.0, "2750"),  # duplicate item_id with cond_3000 entry
        _comp_item("D", 26.0, "2750"),
        _comp_item("E", 24.0, "2750"),
    ]
    fake = _fake_browse_client_seq(
        [{"itemSummaries": cond_3000_items}, {"itemSummaries": cond_2750_items}]
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(
                part_number="ST2000",
                condition="USED",
                own_listing=_own_listing_min(),
                own_live_price=30.0,
            )
        )
    # Raw counts (per-call, BEFORE dedupe): 4 in 3000 (incl. None-itemId), 3 in 2750
    assert result["audit_verbose"]["raw_count_per_condition_id"] == {"3000": 4, "2750": 3}
    # Pre-pipeline merged + deduped pool = 5 unique items (A, B, C, D, E)
    # Note: original issue draft said "merged listings = 4" but correct
    # arithmetic is 5 (3 unique in 3000 + 3 in 2750 with 1 overlap = 5; None
    # excluded by item_id-None skip guard, NOT by pre-dedupe filtering).
    assert result["audit"]["raw_count"] == 5


def test_competitor_prices_opened_fetches_equivalence_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2.3 — OPENED also widens to equivalence class (1500 + 1000)."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    payload_1500 = {"itemSummaries": [_comp_item("o1", 50.0, "1500")]}
    payload_1000 = {"itemSummaries": [_comp_item("n1", 60.0, "1000")]}
    fake = _fake_browse_client_seq([payload_1500, payload_1000])
    own = {**_own_listing_min(), "condition_id": "1500", "condition_name": "Open Box"}
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(
                part_number="ST2000",
                condition="OPENED",
                own_listing=own,
                own_live_price=55.0,
            )
        )
    assert fake.get.call_count == 2
    call_1500, call_1000 = fake.get.call_args_list
    params_1500 = call_1500.kwargs.get("params") or call_1500.args[1]
    params_1000 = call_1000.kwargs.get("params") or call_1000.args[1]
    assert "conditionIds:{1500}" in params_1500["filter"]
    assert "conditionIds:{1000}" in params_1000["filter"]
    assert set(result["audit_verbose"]["raw_count_per_condition_id"].keys()) == {"1500", "1000"}


def test_competitor_prices_new_remains_single_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2.4 — NEW class is N=1 so only one Browse call dispatched."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    fake = _fake_browse_client_seq(
        [{"itemSummaries": [_comp_item("n1", 80.0, "1000")]}]
    )
    own = {**_own_listing_min(), "condition_id": "1000", "condition_name": "New"}
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(
                part_number="ST2000",
                condition="NEW",
                own_listing=own,
                own_live_price=85.0,
            )
        )
    assert fake.get.call_count == 1
    params = fake.get.call_args.kwargs.get("params") or fake.get.call_args.args[1]
    assert "conditionIds:{1000}" in params["filter"]
    assert result["audit_verbose"]["raw_count_per_condition_id"] == {"1000": 1}


def test_competitor_prices_for_parts_unmapped_class_falls_back_to_single(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2.5 — FOR_PARTS (cond_id=7000) has no YAML entry → fallback to [primary]."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    fake = _fake_browse_client_seq(
        [{"itemSummaries": [_comp_item("p1", 5.0, "7000")]}]
    )
    own = {**_own_listing_min(), "condition_id": "7000", "condition_name": "For parts"}
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(
                part_number="ST2000",
                condition="FOR_PARTS",
                own_listing=own,
                own_live_price=10.0,
            )
        )
    assert fake.get.call_count == 1
    params = fake.get.call_args.kwargs.get("params") or fake.get.call_args.args[1]
    assert "conditionIds:{7000}" in params["filter"]
    assert result["audit_verbose"]["raw_count_per_condition_id"] == {"7000": 1}


def test_competitor_prices_empty_class_falls_back_to_single(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """B2.6 — empty-but-present YAML list falls back to [primary] via `or [primary]` guard."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    # YAML override with empty equivalence list for "3000".
    yaml_text = """
comp_filter:
  condition_equivalence:
    "3000": []
  hard_reject_patterns: {}
  caddy_mismatch_patterns: []
  series_names: []
  quality_thresholds:
    require_at_least_one_image: true
"""
    cfg_path = tmp_path / "filter_empty_class.yaml"
    cfg_path.write_text(yaml_text)
    monkeypatch.setenv("EBAY_FILTER_CONFIG", str(cfg_path))
    browse.reset_filter_cache()  # force re-read

    fake = _fake_browse_client_seq(
        [{"itemSummaries": [_comp_item("x1", 30.0, "3000")]}]
    )
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(
            browse.fetch_competitor_prices(part_number="ST2000", condition="USED")
        )
    assert fake.get.call_count == 1
    params = fake.get.call_args.kwargs.get("params") or fake.get.call_args.args[1]
    assert "conditionIds:{3000}" in params["filter"]
    assert result["count"] == 1


def test_competitor_prices_currency_safety_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2.7 — mixed currencies across the equivalence-class calls still raises."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    payload_gbp = {
        "itemSummaries": [
            {**_comp_item("g1", 30.0, "3000"), "price": {"value": "30.00", "currency": "GBP"}}
        ]
    }
    payload_usd = {
        "itemSummaries": [
            {**_comp_item("u1", 32.0, "2750"), "price": {"value": "32.00", "currency": "USD"}}
        ]
    }
    fake = _fake_browse_client_seq([payload_gbp, payload_usd])
    with patch("ebay.browse.get_browse_session", return_value=fake):
        mixed_pat = r"mixed currencies.*GBP.*USD|mixed currencies.*USD.*GBP"
        with pytest.raises(ValueError, match=mixed_pat):
            _run(browse.fetch_competitor_prices(part_number="ST2000", condition="USED"))


def test_competitor_prices_partial_failure_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2.8 — call 1 OK + call 2 5xx → raise_for_ebay_error fires, no partial result."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    resp_ok = _fake_response({"itemSummaries": [_comp_item("a1", 30.0, "3000")]})
    resp_500 = _fake_response({"errors": [{"message": "boom"}]}, status_code=500)
    client = MagicMock()
    client.get.side_effect = [resp_ok, resp_500]
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    with patch("ebay.browse.get_browse_session", return_value=client):
        with pytest.raises(Exception):  # noqa: B017 — raise_for_ebay_error wraps; any exception OK
            _run(browse.fetch_competitor_prices(part_number="ST2000", condition="USED"))
    # Both calls dispatched; second crashed so no partial result returned
    assert client.get.call_count == 2


def test_competitor_prices_reset_filter_cache_isolates_yaml_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """B2.11 — multi-call tests with EBAY_FILTER_CONFIG must reset the lru_cache.

    Smoke test demonstrating the discipline: write a YAML with an unusual
    equivalence class, swap it in via env, reset cache, verify the orchestrator
    actually uses the new mapping (not stale cached production YAML).
    """
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    yaml_text = """
comp_filter:
  condition_equivalence:
    "1000":
      - "1000"
      - "1500"
      - "2000"
  hard_reject_patterns: {}
  caddy_mismatch_patterns: []
  series_names: []
  quality_thresholds:
    require_at_least_one_image: true
"""
    cfg_path = tmp_path / "filter_unusual_class.yaml"
    cfg_path.write_text(yaml_text)
    monkeypatch.setenv("EBAY_FILTER_CONFIG", str(cfg_path))
    browse.reset_filter_cache()

    payloads = [
        {"itemSummaries": [_comp_item("a", 80.0, "1000")]},
        {"itemSummaries": [_comp_item("b", 70.0, "1500")]},
        {"itemSummaries": [_comp_item("c", 60.0, "2000")]},
    ]
    fake = _fake_browse_client_seq(payloads)
    with patch("ebay.browse.get_browse_session", return_value=fake):
        result = _run(browse.fetch_competitor_prices(part_number="ST2000", condition="NEW"))
    # YAML override expanded NEW to 3 calls (1000 + 1500 + 2000)
    assert fake.get.call_count == 3
    params_seq = [c.kwargs.get("params") or c.args[1] for c in fake.get.call_args_list]
    filters = [p["filter"] for p in params_seq]
    assert any("conditionIds:{1000}" in f for f in filters)
    assert any("conditionIds:{1500}" in f for f in filters)
    assert any("conditionIds:{2000}" in f for f in filters)
    assert result["count"] == 3
