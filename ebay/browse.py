"""
Browse API wrapper (Issue #4 Phase 3 + Issue #13 Phase 1.1/1.2).

Uses app-token (client_credentials grant) via ebay/oauth.py get_browse_session().
Seller exclusion is client-side (Browse supports sellers:{include-only}, no
exclude filter) — own seller username sourced from EBAY_OWN_SELLER_USERNAME
env var (private; NOT in public YAML).

Per-listing dict (Issue #13 1.1/1.2 extensions):
    item_id, title, price, currency, seller, condition, url
        — original Issue #4 fields.
    item_creation_date — ISO 8601 UTC string (`itemCreationDate`).
        Defensive lookup: `item.get("itemCreationDate")`. None when absent.
    image_url — primary thumbnail (`image.imageUrl`).
        Defensive: `(item.get("image") or {}).get("imageUrl")`. None when absent.
    additional_image_count — count of secondary images (`additionalImages`).
        Defensive: `len(item.get("additionalImages") or [])`. 0 when absent.
    seller_feedback_pct — seller positive feedback %.
        Defensive: `(item.get("seller") or {}).get("feedbackPercentage")`.
    seller_feedback_score — seller feedback count.
        Defensive: `(item.get("seller") or {}).get("feedbackScore")`.
    top_rated — Top Rated buying experience flag.
        Defensive: `item.get("topRatedBuyingExperience")`. None when absent.
    returns_accepted — bool.
        Defensive: `(item.get("returnTerms") or {}).get("returnsAccepted")`.
    returns_within_days — int (e.g. 30).
        Defensive: `(item.get("returnTerms") or {}).get("returnsWithinDays")`.

All defensive lookups: missing keys yield None (or 0 for additional_image_count) — never raise.
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

        seller_obj = item.get("seller") or {}
        return_terms = item.get("returnTerms") or {}
        image_obj = item.get("image") or {}
        listings.append(
            {
                "item_id": item.get("itemId"),
                "title": item.get("title"),
                "price": price_val,
                "currency": price_obj.get("currency"),
                "seller": seller,
                "condition": cond,
                "url": item.get("itemWebUrl"),
                "item_creation_date": item.get("itemCreationDate"),
                "image_url": image_obj.get("imageUrl"),
                "additional_image_count": len(item.get("additionalImages") or []),
                "seller_feedback_pct": seller_obj.get("feedbackPercentage"),
                "seller_feedback_score": seller_obj.get("feedbackScore"),
                "top_rated": item.get("topRatedBuyingExperience"),
                "returns_accepted": return_terms.get("returnsAccepted"),
                "returns_within_days": return_terms.get("returnsWithinDays"),
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


# Issue #13 Phase 2.1 — apple-to-apples competitor scoring + clean filter.
#
# Bundle keywords that disqualify a comp listing from being a price comparable
# (these are NOT the product the buyer is searching for, so their price can
# either inflate the median (kit upsells) or deflate it (joblot dumps)).
_BUNDLE_KEYWORDS: tuple[str, ...] = (
    "caddy",
    "cables",
    "warranty",
    "kit",
    " lot ",
    "bundle",
    "joblot",
    "job lot",
)


def _parse_iso_age_days(creation_date: str | None) -> int | None:
    """Parse ISO 8601 timestamp → integer days since now (UTC). None on parse failure."""
    if not creation_date:
        return None
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(str(creation_date).replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except (ValueError, TypeError):
        return None


def score_apple_to_apple(own_listing: dict[str, Any], comp_item: dict[str, Any]) -> float:
    """Score a competitor listing 0.0-1.0 across 5 dimensions × 0.2.

    Dimensions:
      1. Exact MPN substring in comp title (case-insensitive)
      2. Comp title free of bundle keywords (caddy / kit / bundle / lot / etc.)
      3. Form-factor match (own_listing.specifics["Form Factor"] in comp title)
      4. Condition tier exact match (own.condition_name == comp.condition)
      5. Listing age <200 days from item_creation_date
         (skip → score 0.2 by default if date missing — spec 2.1.1)

    Score 0.6+ is the suggested threshold for `filter_clean_competitors`.

    Args:
        own_listing: dict from listing_to_dict() — must have specifics +
            condition_name. Defensive: missing keys do NOT raise.
        comp_item: dict from fetch_competitor_prices() listings[] — must
            have title, condition, item_creation_date. Defensive.

    Returns:
        float in [0.0, 1.0], rounded to 2dp.
    """
    score = 0.0
    own_specifics = own_listing.get("specifics") or {}
    comp_title = str(comp_item.get("title") or "")
    comp_title_upper = comp_title.upper()
    comp_title_lower = comp_title.lower()

    # Dim 1 — MPN exact substring.
    mpns = own_specifics.get("MPN") or []
    if mpns and any(str(m).upper().strip() and str(m).upper().strip() in comp_title_upper
                    for m in mpns):
        score += 0.2

    # Dim 2 — comp title is NOT a bundle.
    if not any(kw in comp_title_lower for kw in _BUNDLE_KEYWORDS):
        score += 0.2

    # Dim 3 — form factor match (e.g. "3.5\"" present in comp title).
    own_ff_list = own_specifics.get("Form Factor") or []
    if own_ff_list:
        norm_comp = comp_title_lower.replace('"', "").replace("'", "").replace(" ", "")
        for ff in own_ff_list:
            ff_norm = str(ff).lower().replace('"', "").replace("'", "").replace(" ", "")
            if ff_norm and ff_norm in norm_comp:
                score += 0.2
                break

    # Dim 4 — condition tier exact match.
    own_cond = own_listing.get("condition_name")
    comp_cond = comp_item.get("condition")
    if own_cond and comp_cond and str(own_cond).strip() == str(comp_cond).strip():
        score += 0.2

    # Dim 5 — listing age <200 days. Missing date = +0.2 (per spec 2.1.1).
    creation = comp_item.get("item_creation_date")
    if creation is None:
        score += 0.2
    else:
        age = _parse_iso_age_days(creation)
        if age is not None and age < 200:
            score += 0.2

    return round(score, 2)


def filter_clean_competitors(
    own_listing: dict[str, Any],
    comp_listings: list[dict[str, Any]],
    threshold: float = 0.6,
) -> list[dict[str, Any]]:
    """Keep only competitor listings scoring >= threshold (default 0.6)."""
    return [c for c in comp_listings if score_apple_to_apple(own_listing, c) >= threshold]


def drop_stale_competitors(
    comp_listings: list[dict[str, Any]],
    drop_pct: float = 10.0,
) -> list[dict[str, Any]]:
    """Drop the oldest `drop_pct`% by item_creation_date (per round-2 F-F).

    Listings with missing item_creation_date are RETAINED (we can't tell if
    they're stale; filter_clean_competitors already penalises them via the
    age dimension).

    Run AFTER `filter_clean_competitors` and BEFORE percentile computation.
    """
    if drop_pct <= 0 or not comp_listings:
        return list(comp_listings)
    with_dates: list[tuple[dict[str, Any], int]] = []
    undated: list[dict[str, Any]] = []
    for c in comp_listings:
        age = _parse_iso_age_days(c.get("item_creation_date"))
        if age is None:
            undated.append(c)
        else:
            with_dates.append((c, age))
    if not with_dates:
        return list(comp_listings)
    # Oldest first (largest age days).
    with_dates.sort(key=lambda x: x[1], reverse=True)
    drop_count = int(len(with_dates) * drop_pct / 100.0)
    kept = [c for c, _ in with_dates[drop_count:]]
    return undated + kept
