"""
Browse API wrapper (Issue #4 Phase 3).

Uses app-token (client_credentials grant) via ebay/oauth.py get_browse_session().
Seller exclusion is client-side (Browse supports sellers:{include-only}, no
exclude filter) — own seller username sourced from EBAY_OWN_SELLER_USERNAME
env var (private; NOT in public YAML).
"""

from __future__ import annotations

import asyncio
import os
import statistics
from typing import Any

from ebay.oauth import get_browse_session, raise_for_ebay_error


def _condition_id_for(condition: str) -> str:
    """Map condition string to eBay conditionId (category-independent mapping)."""
    mapping = {
        "NEW": "1000",
        "USED": "3000",
        "USED_EXCELLENT": "2750",
        "OPENED": "1500",
        "FOR_PARTS": "7000",
    }
    key = condition.upper().strip()
    if key not in mapping:
        raise ValueError(f"Unknown condition {condition!r}. Valid: {list(mapping.keys())}")
    return mapping[key]


def _own_seller_lower() -> str | None:
    user = os.environ.get("EBAY_OWN_SELLER_USERNAME")
    return user.lower() if user else None


def _sync_find_competitor_prices(
    part_number: str,
    condition: str,
    location_country: str,
    limit: int,
) -> dict[str, Any]:
    if not part_number or not part_number.strip():
        raise ValueError("part_number required")
    cond_id = _condition_id_for(condition)

    params = {
        "q": part_number,
        "filter": (
            f"buyingOptions:{{FIXED_PRICE}},"
            f"conditionIds:{{{cond_id}}},"
            f"itemLocationCountry:{{{location_country}}}"
        ),
        "limit": str(min(max(limit, 1), 200)),
    }
    with get_browse_session() as client:
        response = client.get("/buy/browse/v1/item_summary/search", params=params)
    raise_for_ebay_error(response)
    payload = response.json()

    own = _own_seller_lower()
    raw_listings = payload.get("itemSummaries", []) or []

    listings: list[dict[str, Any]] = []
    prices: list[float] = []
    shipping_free_count = 0
    best_offer_count = 0
    promoted_count = 0
    by_condition: dict[str, int] = {}
    currencies_seen: set[str] = set()

    for item in raw_listings:
        seller = (item.get("seller") or {}).get("username", "")
        if own and seller.lower() == own:
            continue
        price_obj = item.get("price") or {}
        try:
            price_val = float(price_obj.get("value"))
        except (TypeError, ValueError):
            continue
        currencies_seen.add(price_obj.get("currency", "GBP"))
        prices.append(price_val)

        shipping_options = item.get("shippingOptions") or []
        if shipping_options:
            first_ship = shipping_options[0].get("shippingCost", {})
            try:
                if float(first_ship.get("value", 0)) == 0.0:
                    shipping_free_count += 1
            except (TypeError, ValueError):
                pass

        if item.get("bestOfferEnabled"):
            best_offer_count += 1
        if item.get("listingMarketplaceId") and "PROMOTED" in str(
            item.get("itemAffiliateWebUrl", "")
        ):
            promoted_count += 1
        cond = item.get("condition", "UNKNOWN")
        by_condition[cond] = by_condition.get(cond, 0) + 1

        listings.append(
            {
                "item_id": item.get("itemId"),
                "title": item.get("title"),
                "price": price_val,
                "currency": price_obj.get("currency"),
                "seller": seller,
                "condition": cond,
                "url": item.get("itemWebUrl"),
            }
        )

    if len(currencies_seen) > 1:
        raise ValueError(
            f"Browse API response mixed currencies {sorted(currencies_seen)}; "
            f"refusing to aggregate. Filter `itemLocationCountry` should prevent this."
        )
    currency = next(iter(currencies_seen)) if currencies_seen else "GBP"

    count = len(listings)
    if count == 0:
        return {
            "count": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "max": None,
            "currency": currency,
            "by_condition_dict": by_condition,
            "shipping_free_pct": None,
            "best_offer_enabled_pct": None,
            "promoted_pct": None,
            "listings": [],
        }

    sorted_prices = sorted(prices)
    return {
        "count": count,
        "min": round(sorted_prices[0], 2),
        "p25": round(sorted_prices[max(0, count // 4)], 2),
        "median": round(statistics.median(sorted_prices), 2),
        "p75": round(sorted_prices[min(count - 1, (3 * count) // 4)], 2),
        "max": round(sorted_prices[-1], 2),
        "currency": currency,
        "by_condition_dict": by_condition,
        "shipping_free_pct": round(100.0 * shipping_free_count / count, 1),
        "best_offer_enabled_pct": round(100.0 * best_offer_count / count, 1),
        "promoted_pct": round(100.0 * promoted_count / count, 1),
        "listings": listings,
    }


async def fetch_competitor_prices(
    part_number: str,
    condition: str = "USED",
    location_country: str = "GB",
    limit: int = 50,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _sync_find_competitor_prices, part_number, condition, location_country, limit
    )
