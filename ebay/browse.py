"""
Browse API wrapper + apple-to-apples competitor filter pipeline.

Issue #4 Phase 3 introduced the basic Browse fetch.
Issue #13 added scoring + clean filter + stale-drop.
Issue #14 redesigned the filter as a three-layer pipeline:
    Layer 1 — `filter_low_quality_competitors`: hard rejects on binary
              quality signals + regex categories (broken/parts, external
              form-factor, wrong category, bundle) + own-listing-aware
              caddy/series-name mismatch.
    Layer 2 — `score_apple_to_apple` + `filter_clean_competitors`: structural
              4-dim score (MPN / form-factor / numeric-conditionId-equivalence /
              age <200d) plus soft Layer-2 deductions for poor seller signals
              (feedback %, feedback score, returns, top-rated).
    Layer 3 — `drop_price_outliers`: log-space IQR fence with three guards
              (min_pool_size, max_drop_frac, own-price-anchored sanity).

Per-listing dict (Issue #13 1.1/1.2 + Issue #14 0.5/2.1 extensions):
    item_id, title, price, currency, seller, condition, url
        — original Issue #4 fields.
    item_creation_date — ISO 8601 UTC string (`itemCreationDate`).
    image_url — primary thumbnail (`image.imageUrl`).
    additional_image_count — count of secondary images (`additionalImages`).
    seller_feedback_pct — seller positive feedback %.
    seller_feedback_score — seller feedback count.
    top_rated — Top Rated buying experience flag.
    returns_accepted — bool.
    returns_within_days — int (e.g. 30).
    best_offer_enabled — per-listing best-offer flag (Phase 7 plumbing).
    condition_id — numeric eBay conditionId as string (Issue #14 Phase 2.1).
    shipping_cost — first shipping option cost as float (Issue #14 Phase 0.5).

All defensive lookups: missing keys yield None (or 0 for additional_image_count) — never raise.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import statistics
from functools import lru_cache
from typing import Any

import yaml

from ebay.oauth import get_browse_session, raise_for_ebay_error

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_FILTER_CONFIG = os.path.join(_REPO_ROOT, "config", "pricing_and_content.yaml")


# Issue #14 Phase 2.4 — Browse search uses single conditionId per call.
#
# Live curl verification 2026-04-25 against eBay Browse v1: pipe-separator
# syntax `conditionIds:{3000|2750}` is silently TRUNCATED by the API to
# `conditionIds:{3000}` (first ID only). Comma-separator returns unrelated
# results (1000/2010/3000 mix). Conclusion: Browse `conditionIds` accepts
# exactly ONE ID per filter string. To widen the comp pool to the equivalence
# class, callers must run two sequential `fetch_competitor_prices` calls and
# merge dedupe by item_id (planned as Phase 6.4 follow-up).
#
# Score-side equivalence still bridges 3000↔2750 in `score_apple_to_apple`
# for any 2750 listings that happen to surface from a multi-MPN search where
# the OEM part returns a Used-Excellent variant.
_BROWSE_CONDITION_FILTERS: dict[str, str] = {
    "NEW": "1000",
    "USED": "3000",
    "USED_EXCELLENT": "2750",
    "OPENED": "1500",
    "FOR_PARTS": "7000",
}


def _condition_id_for(condition: str) -> str:
    """Map condition string to eBay conditionIds filter (single-ID per call).

    See module-level note above for why pipe-separator was reverted post-live-verification.
    """
    key = condition.upper().strip()
    if key not in _BROWSE_CONDITION_FILTERS:
        raise ValueError(
            f"Unknown condition {condition!r}. Valid: {list(_BROWSE_CONDITION_FILTERS.keys())}"
        )
    return _BROWSE_CONDITION_FILTERS[key]


@lru_cache(maxsize=1)
def _load_filter_config() -> dict[str, Any]:
    """Load and cache pricing_and_content.yaml. Override via EBAY_FILTER_CONFIG env (tests)."""
    path = os.environ.get("EBAY_FILTER_CONFIG", _DEFAULT_FILTER_CONFIG)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"comp_filter config missing: {path} — expected at config/pricing_and_content.yaml"
        )
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "comp_filter" not in data:
        raise ValueError(f"{path}: missing 'comp_filter' top-level section (Issue #14)")
    return data


def reset_filter_cache() -> None:
    """Clear cached filter config — tests that swap EBAY_FILTER_CONFIG call this."""
    _load_filter_config.cache_clear()
    _compiled_hard_reject_patterns.cache_clear()
    _compiled_caddy_patterns.cache_clear()


@lru_cache(maxsize=1)
def _compiled_hard_reject_patterns() -> dict[str, list[re.Pattern[str]]]:
    """Compile Phase 1.3 regex categories ONCE per process.

    Returns ``{category_name: [compiled_pattern, ...]}``. Same Pattern object
    is returned across calls — identity-checked by the regex-cache test.
    """
    cfg = _load_filter_config()
    patterns_block = cfg.get("comp_filter", {}).get("hard_reject_patterns", {}) or {}
    compiled: dict[str, list[re.Pattern[str]]] = {}
    for category, raw_patterns in patterns_block.items():
        compiled[category] = [re.compile(p, re.IGNORECASE) for p in (raw_patterns or [])]
    return compiled


@lru_cache(maxsize=1)
def _compiled_caddy_patterns() -> list[re.Pattern[str]]:
    """Compile Phase 3.1 caddy-mismatch regex ONCE per process."""
    cfg = _load_filter_config()
    raw = cfg.get("comp_filter", {}).get("caddy_mismatch_patterns", []) or []
    return [re.compile(p, re.IGNORECASE) for p in raw]


def _own_seller_lower() -> str | None:
    user = os.environ.get("EBAY_OWN_SELLER_USERNAME")
    return user.lower() if user else None


def _first_shipping_cost(item: dict[str, Any]) -> float | None:
    """Extract first shippingOption cost as float; None on missing/parse failure."""
    options = item.get("shippingOptions") or []
    if not options:
        return None
    raw = (options[0].get("shippingCost") or {}).get("value")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _sync_find_competitor_prices(
    part_number: str,
    condition: str,
    location_country: str,
    limit: int,
    own_listing: dict[str, Any] | None = None,
    own_live_price: float | None = None,
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
        # Defensive: NaN/Inf parses successfully via float() but breaks
        # downstream json.dumps + percentile arithmetic. Browse API never
        # emits these, but guard at the boundary anyway.
        if not math.isfinite(price_val):
            continue
        currencies_seen.add(price_obj.get("currency", "GBP"))
        prices.append(price_val)

        shipping_cost = _first_shipping_cost(item)
        if shipping_cost is not None and shipping_cost == 0.0:
            shipping_free_count += 1

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
                # Issue #14 Phase 2.1 — numeric conditionId for equivalence-class match.
                "condition_id": (
                    str(item.get("conditionId")) if item.get("conditionId") is not None else None
                ),
                "url": item.get("itemWebUrl"),
                "item_creation_date": item.get("itemCreationDate"),
                "image_url": image_obj.get("imageUrl"),
                "additional_image_count": len(item.get("additionalImages") or []),
                "seller_feedback_pct": seller_obj.get("feedbackPercentage"),
                "seller_feedback_score": seller_obj.get("feedbackScore"),
                "top_rated": item.get("topRatedBuyingExperience"),
                "returns_accepted": return_terms.get("returnsAccepted"),
                "returns_within_days": return_terms.get("returnsWithinDays"),
                "best_offer_enabled": bool(item.get("bestOfferEnabled")),
                # Issue #14 Phase 0.5 — per-listing shipping cost (was aggregate-only).
                "shipping_cost": shipping_cost,
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
        empty: dict[str, Any] = {
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
        if own_listing is not None:
            empty["audit"] = {
                "raw_count": 0,
                "kept": 0,
                "dropped_low_quality": 0,
                "dropped_apple_to_apples": 0,
                "dropped_stale": 0,
                "dropped_outlier": 0,
            }
            empty["verdict"] = "ZERO_COMPS_AFTER_FILTER"
        return empty

    sorted_prices = sorted(prices)
    raw_distribution = {
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

    # Issue #14 Phase 5.1 — when own_listing context provided, run the 3-layer
    # pipeline (filter_low_quality_competitors → filter_clean_competitors →
    # drop_stale_competitors → drop_price_outliers) and surface the audit dict
    # + filtered listings + recomputed percentiles. Otherwise return raw shape
    # for backward-compat with callers that only need the bare API response.
    if own_listing is None:
        return raw_distribution

    fees_outlier_cfg: dict[str, Any] = {}
    try:
        # noqa: PLC0415 — lazy import to keep ebay.fees decoupled at module load.
        from ebay.fees import _load_fees_config  # noqa: PLC0415

        fees_outlier_cfg = _load_fees_config().get("outlier_rejection", {}) or {}
    except (FileNotFoundError, ValueError, ImportError):
        fees_outlier_cfg = {}

    kept, audit_flat, audit_verbose = run_comp_filter_pipeline(
        listings,
        own_listing=own_listing,
        threshold=0.6,
        stale_drop_pct=10.0,
        outlier_config=fees_outlier_cfg,
        own_live_price=own_live_price,
    )

    if not kept:
        return {
            **raw_distribution,
            "count": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "max": None,
            "listings": [],
            "audit": audit_flat,
            "audit_verbose": audit_verbose,
            "verdict": "ZERO_COMPS_AFTER_FILTER",
        }

    kept_prices = sorted(c["price"] for c in kept)
    kept_n = len(kept_prices)
    return {
        **raw_distribution,
        "count": kept_n,
        "min": round(kept_prices[0], 2),
        "p25": round(kept_prices[max(0, kept_n // 4)], 2),
        "median": round(statistics.median(kept_prices), 2),
        "p75": round(kept_prices[min(kept_n - 1, (3 * kept_n) // 4)], 2),
        "max": round(kept_prices[-1], 2),
        "listings": kept,
        "audit": audit_flat,
        "audit_verbose": audit_verbose,
    }


async def fetch_competitor_prices(
    part_number: str,
    condition: str = "USED",
    location_country: str = "GB",
    limit: int = 50,
    own_listing: dict[str, Any] | None = None,
    own_live_price: float | None = None,
) -> dict[str, Any]:
    """Browse API competitor scan with optional in-pipeline filtering.

    When ``own_listing`` is provided, runs the Issue #14 three-layer comp-filter
    pipeline (low-quality reject → apple-to-apples score → stale-drop → outlier-
    drop) before returning. The result dict gains ``audit`` (flat 6-key) and
    ``audit_verbose`` (per-reason histogram), and ``count``/``min``/``p25``/
    ``median``/``p75``/``max``/``listings`` reflect the kept comps. When all
    comps are dropped, ``verdict: 'ZERO_COMPS_AFTER_FILTER'`` is surfaced.

    When ``own_listing`` is None, returns the raw distribution (backward compat
    for callers that don't have own-listing context).
    """
    return await asyncio.to_thread(
        _sync_find_competitor_prices,
        part_number,
        condition,
        location_country,
        limit,
        own_listing,
        own_live_price,
    )


# ---------------------------------------------------------------------------
# Layer 1 — hard pre-rejection (Issue #14 Phase 1)
# ---------------------------------------------------------------------------


def _own_has_caddy(own_listing: dict[str, Any] | None) -> bool:
    """Detect whether own_listing has a caddy.

    Three sources per skill canonical (SKILL.md:552, 562, 678):
      1. ``own_listing.title`` contains ``+Caddy`` (canonical token, SKILL.md:552)
      2. ``own_listing.specifics["Features"]`` contains ``Caddy`` or ``Hot Swap`` (SKILL.md:678)
      3. ``own_listing.has_caddy`` boolean (runtime arg from create_listing flow, SKILL.md:562)
    """
    if not own_listing:
        return False
    if own_listing.get("has_caddy") is True:
        return True
    title = str(own_listing.get("title") or "")
    if "+caddy" in title.lower():
        return True
    features = (own_listing.get("specifics") or {}).get("Features") or []
    if isinstance(features, str):
        features = [features]
    feature_blob = " ".join(str(f).lower() for f in features)
    if "caddy" in feature_blob or "hot swap" in feature_blob:
        return True
    return False


def _own_series_name(own_listing: dict[str, Any] | None) -> str | None:
    """Return the series-name token from own_listing.title if any (case-insensitive).

    Word-boundary matching prevents bare English words like ``"red"`` and ``"gold"``
    from false-positive-matching titles such as ``"Sealed Box - Red Label"``. Returns
    the canonical lower-case form from ``comp_filter.series_names``. Longest match
    wins (e.g. ``"iron wolf pro"`` beats ``"iron wolf"``).
    """
    if not own_listing:
        return None
    title = str(own_listing.get("title") or "").lower()
    if not title:
        return None
    cfg = _load_filter_config()
    series_names = cfg.get("comp_filter", {}).get("series_names", []) or []
    matched: list[str] = []
    for name in series_names:
        name_lc = name.lower()
        if re.search(r"\b" + re.escape(name_lc) + r"\b", title):
            matched.append(name_lc)
    if not matched:
        return None
    matched.sort(key=len, reverse=True)
    return matched[0]


def filter_low_quality_competitors(
    comp_listings: list[dict[str, Any]],
    own_listing: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Layer-1 hard pre-rejection. Run BEFORE ``filter_clean_competitors``.

    Returns ``(survivors, audit)``. Audit shape:
        {"raw": int, "kept": int, "dropped_reasons": {category: count}}

    Drop categories:
      - image_zero — image_url is None AND additional_image_count == 0
      - broken_or_parts — Phase 1.3 regex
      - external_or_wrong_form_factor — Phase 1.3 regex
      - wrong_category — Phase 1.3 regex
      - bundle — Phase 1.3 regex (replaces _BUNDLE_KEYWORDS substring match)
      - caddy_mismatch — own_listing has caddy AND comp matches caddy-mismatch regex
      - series_mismatch — own_listing.title contains a series name AND comp title doesn't
    """
    cfg = _load_filter_config()
    quality = cfg.get("comp_filter", {}).get("quality_thresholds", {}) or {}
    require_image = quality.get("require_at_least_one_image", True)

    hard_reject = _compiled_hard_reject_patterns()
    caddy_patterns = _compiled_caddy_patterns()
    own_has_caddy = _own_has_caddy(own_listing)
    own_series = _own_series_name(own_listing)

    survivors: list[dict[str, Any]] = []
    drops: dict[str, int] = {}

    for comp in comp_listings:
        title = str(comp.get("title") or "")
        title_lower = title.lower()

        if require_image and (
            comp.get("image_url") is None and (comp.get("additional_image_count") or 0) == 0
        ):
            drops["image_zero"] = drops.get("image_zero", 0) + 1
            continue

        category_hit: str | None = None
        for category, patterns in hard_reject.items():
            if any(p.search(title) for p in patterns):
                category_hit = category
                break
        if category_hit is not None:
            drops[category_hit] = drops.get(category_hit, 0) + 1
            continue

        if own_has_caddy and any(p.search(title) for p in caddy_patterns):
            drops["caddy_mismatch"] = drops.get("caddy_mismatch", 0) + 1
            continue

        if own_series and not re.search(r"\b" + re.escape(own_series) + r"\b", title_lower):
            drops["series_mismatch"] = drops.get("series_mismatch", 0) + 1
            continue

        survivors.append(comp)

    audit = {
        "raw": len(comp_listings),
        "kept": len(survivors),
        "dropped_reasons": drops,
    }
    return survivors, audit


# ---------------------------------------------------------------------------
# Layer 2 — apple-to-apples scoring (Issue #14 Phase 2.3 + 3.4)
# ---------------------------------------------------------------------------


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


def _safe_float(value: Any, default: float | None = None) -> float | None:
    """Defensive float cast. seller_feedback_pct arrives as string from Browse API."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def score_apple_to_apple(own_listing: dict[str, Any], comp_item: dict[str, Any]) -> float:
    """Score a competitor listing 0.0-1.0 across 4 structural dims × 0.25 minus Layer-2 deductions.

    Issue #14 redesign:
      Dim 1 — Exact MPN substring in comp title (case-insensitive)        +0.25
      Dim 2 — Form-factor match (own.specifics["Form Factor"] in title)   +0.25
      Dim 3 — Numeric conditionId equivalence-class match (Phase 2.3)     +0.25
      Dim 4 — Listing age <200 days (None = +0.0 per Phase 0.4)           +0.25

      Layer-2 soft deductions (each fires once when the comp signals poor quality):
        seller_feedback_pct < soft_min      → -deduction.seller_feedback_pct
        seller_feedback_score < soft_min    → -deduction.seller_feedback_score
        returns_accepted is False (explicit) → -deduction.returns_accepted
        returns_within_days < soft_min       → -deduction.returns_within_days
        top_rated is not True               → -deduction.top_rated

    Score 0.6+ is the suggested threshold for ``filter_clean_competitors``.

    Args:
        own_listing: dict from listing_to_dict(). Must have specifics +
            condition_id (preferred) or condition_name. Defensive: missing
            keys do NOT raise.
        comp_item: dict from fetch_competitor_prices() listings[]. Must have
            title, condition_id, item_creation_date. Defensive.

    Returns:
        float in [0.0, 1.0], rounded to 2dp. Negative scores clamped to 0.0.
    """
    score = 0.0
    own_specifics = own_listing.get("specifics") or {}
    comp_title = str(comp_item.get("title") or "")
    comp_title_upper = comp_title.upper()
    comp_title_lower = comp_title.lower()

    # Dim 1 — MPN exact substring (case-insensitive).
    mpns = own_specifics.get("MPN") or []
    if mpns and any(
        str(m).upper().strip() and str(m).upper().strip() in comp_title_upper for m in mpns
    ):
        score += 0.25

    # Dim 2 — form factor match (e.g. "3.5\"" or "2.5\"" present).
    own_ff_list = own_specifics.get("Form Factor") or []
    if own_ff_list:
        norm_comp = comp_title_lower.replace('"', "").replace("'", "").replace(" ", "")
        for ff in own_ff_list:
            ff_norm = str(ff).lower().replace('"', "").replace("'", "").replace(" ", "")
            if ff_norm and ff_norm in norm_comp:
                score += 0.25
                break

    # Dim 3 — numeric conditionId equivalence-class (Phase 2.3).
    cfg = _load_filter_config()
    equivalence = cfg.get("comp_filter", {}).get("condition_equivalence", {}) or {}
    own_cond_id = own_listing.get("condition_id")
    comp_cond_id = comp_item.get("condition_id")
    if own_cond_id and comp_cond_id:
        own_cond_str = str(own_cond_id)
        accepted = set(equivalence.get(own_cond_str, [own_cond_str]))
        if str(comp_cond_id) in accepted:
            score += 0.25

    # Dim 4 — listing age <200 days. Phase 0.4: missing date = 0.0 (was 0.2).
    creation = comp_item.get("item_creation_date")
    if creation is not None:
        age = _parse_iso_age_days(creation)
        if age is not None and age < 200:
            score += 0.25

    # Layer-2 soft deductions (Phase 3.4) — read thresholds + amounts from config.
    quality = cfg.get("comp_filter", {}).get("quality_thresholds", {}) or {}
    deductions = cfg.get("comp_filter", {}).get("quality_deductions", {}) or {}

    feedback_pct = _safe_float(comp_item.get("seller_feedback_pct"))
    soft_min_pct = quality.get("soft_min_seller_feedback_pct")
    if feedback_pct is not None and soft_min_pct is not None and feedback_pct < soft_min_pct:
        score -= float(deductions.get("seller_feedback_pct", 0.0))

    # Defensive _safe_float cast: Browse API typically returns int, but parity
    # with seller_feedback_pct (which is a string field) prevents silent bypass
    # when an upstream layer stringifies the score.
    feedback_score = _safe_float(comp_item.get("seller_feedback_score"))
    soft_min_score = quality.get("soft_min_seller_feedback_score")
    if (
        feedback_score is not None
        and soft_min_score is not None
        and feedback_score < soft_min_score
    ):
        score -= float(deductions.get("seller_feedback_score", 0.0))

    if (
        quality.get("soft_returns_accepted_required") is True
        and comp_item.get("returns_accepted") is False
    ):
        score -= float(deductions.get("returns_accepted", 0.0))

    rwd = comp_item.get("returns_within_days")
    soft_min_rwd = quality.get("soft_min_returns_within_days")
    if isinstance(rwd, (int, float)) and soft_min_rwd is not None and rwd < soft_min_rwd:
        score -= float(deductions.get("returns_within_days", 0.0))

    if quality.get("soft_top_rated_preferred") is True and comp_item.get("top_rated") is not True:
        score -= float(deductions.get("top_rated", 0.0))

    return round(max(0.0, score), 2)


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
    """Drop the oldest ``drop_pct``% by item_creation_date.

    Listings with missing item_creation_date are RETAINED (we can't tell if
    they're stale; the score function already penalises them via Dim-4).

    Run AFTER ``filter_clean_competitors`` and BEFORE ``drop_price_outliers``.
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
    with_dates.sort(key=lambda x: x[1], reverse=True)
    drop_count = int(len(with_dates) * drop_pct / 100.0)
    kept = [c for c, _ in with_dates[drop_count:]]
    return undated + kept


# ---------------------------------------------------------------------------
# Layer 3 — price-distribution outlier rejection (Issue #14 Phase 4)
# ---------------------------------------------------------------------------


def drop_price_outliers(
    comp_listings: list[dict[str, Any]],
    method: str = "iqr",
    multiplier: float = 1.5,
    log_transform: bool = True,
    min_pool_size: int = 6,
    max_drop_frac: float = 0.20,
    own_live_price: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop price-outlier comps via IQR fence with three guards.

    Guards:
      1. ``min_pool_size`` floor — skip if ``len(comps) < min_pool_size``
      2. ``max_drop_frac`` cap — never drop more than this fraction; rank by
         extremity (distance-from-median) and drop only top-N most extreme.
      3. Own-price-anchored sanity — if ``own_live_price > upper_fence``,
         downgrade rejection to flag (don't drop comps near our own price).

    Args:
        comp_listings: list of comp dicts with float ``price`` field.
        method: "iqr" | "none". "none" returns input unchanged with
            audit.skipped_reason="method_none".
        multiplier: IQR fence multiplier (default 1.5 = standard Tukey fence).
        log_transform: when True, IQR computed in log-space (default True —
            HDD price distributions are right-skewed per Stage 1 F-E).
        min_pool_size: skip threshold (default 6).
        max_drop_frac: max fraction droppable (default 0.20 = 20%).
        own_live_price: own listing live price for anchored-sanity guard.

    Returns:
        ``(kept, audit)``. Audit shape:
            {"raw": int, "kept": int, "dropped": int,
             "skipped_reason": str | None, "fence_lo": float | None,
             "fence_hi": float | None, "log_transform": bool,
             "own_in_outlier_zone": bool}
    """
    audit: dict[str, Any] = {
        "raw": len(comp_listings),
        "kept": len(comp_listings),
        "dropped": 0,
        "skipped_reason": None,
        "fence_lo": None,
        "fence_hi": None,
        "log_transform": log_transform,
        "own_in_outlier_zone": False,
    }
    if method == "none":
        audit["skipped_reason"] = "method_none"
        return list(comp_listings), audit
    if method != "iqr":
        raise ValueError(f"drop_price_outliers: unknown method {method!r}; expected 'iqr'|'none'")

    if len(comp_listings) < min_pool_size:
        audit["skipped_reason"] = "below_min_pool_size"
        return list(comp_listings), audit

    prices: list[float] = []
    for c in comp_listings:
        try:
            prices.append(float(c["price"]))
        except (KeyError, TypeError, ValueError):
            audit["skipped_reason"] = "non_numeric_price"
            return list(comp_listings), audit

    if log_transform and any(p <= 0 for p in prices):
        audit["skipped_reason"] = "non_positive_price_log_transform"
        return list(comp_listings), audit

    transformed = [math.log(p) for p in prices] if log_transform else list(prices)
    sorted_t = sorted(transformed)
    n = len(sorted_t)
    q1 = sorted_t[n // 4]
    q3 = sorted_t[(3 * n) // 4]
    iqr = q3 - q1
    fence_lo_t = q1 - multiplier * iqr
    fence_hi_t = q3 + multiplier * iqr
    fence_lo = math.exp(fence_lo_t) if log_transform else fence_lo_t
    fence_hi = math.exp(fence_hi_t) if log_transform else fence_hi_t
    audit["fence_lo"] = round(fence_lo, 2)
    audit["fence_hi"] = round(fence_hi, 2)

    median_t = sorted_t[n // 2]
    candidates: list[tuple[int, float]] = []  # (index, distance from median in transformed space)
    for idx, p_t in enumerate(transformed):
        if p_t < fence_lo_t or p_t > fence_hi_t:
            candidates.append((idx, abs(p_t - median_t)))

    if own_live_price is not None and own_live_price > fence_hi:
        audit["own_in_outlier_zone"] = True
        return list(comp_listings), audit

    max_drop = int(len(comp_listings) * max_drop_frac)
    candidates.sort(key=lambda x: x[1], reverse=True)
    drop_indices = {idx for idx, _ in candidates[:max_drop]}
    kept = [c for i, c in enumerate(comp_listings) if i not in drop_indices]

    audit["kept"] = len(kept)
    audit["dropped"] = len(comp_listings) - len(kept)
    return kept, audit


# ---------------------------------------------------------------------------
# Issue #14 Phase 5 — full pipeline aggregator with audit dict.
# ---------------------------------------------------------------------------


def run_comp_filter_pipeline(
    raw_listings: list[dict[str, Any]],
    own_listing: dict[str, Any] | None,
    *,
    threshold: float = 0.6,
    stale_drop_pct: float = 10.0,
    outlier_config: dict[str, Any] | None = None,
    own_live_price: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Apply Layer-1 → Layer-2 → stale → Layer-3 in order, returning kept comps + audit dicts.

    Returns ``(kept, audit_flat, audit_verbose)`` where:
      audit_flat: 6-key user-facing summary (Phase 5.1)
      audit_verbose: nested per-reason counters (debug-log only)
    """
    raw_count = len(raw_listings)

    survivors_lq, lq_audit = filter_low_quality_competitors(raw_listings, own_listing=own_listing)
    after_low_quality = len(survivors_lq)

    if own_listing is not None:
        survivors_score = filter_clean_competitors(own_listing, survivors_lq, threshold=threshold)
    else:
        survivors_score = list(survivors_lq)
    after_score = len(survivors_score)

    survivors_stale = drop_stale_competitors(survivors_score, drop_pct=stale_drop_pct)
    after_stale = len(survivors_stale)

    cfg = outlier_config or {}
    if cfg.get("enabled", True) and cfg.get("method", "iqr") != "none":
        survivors_outlier, outlier_audit = drop_price_outliers(
            survivors_stale,
            method=cfg.get("method", "iqr"),
            multiplier=float(cfg.get("multiplier", 1.5)),
            log_transform=bool(cfg.get("log_transform", True)),
            min_pool_size=int(cfg.get("min_pool_size", 6)),
            max_drop_frac=float(cfg.get("max_drop_frac", 0.20)),
            own_live_price=own_live_price,
        )
    else:
        survivors_outlier = list(survivors_stale)
        outlier_audit = {
            "raw": after_stale,
            "kept": after_stale,
            "dropped": 0,
            "skipped_reason": "disabled" if cfg.get("enabled") is False else "method_none",
            "fence_lo": None,
            "fence_hi": None,
            "log_transform": bool(cfg.get("log_transform", True)),
            "own_in_outlier_zone": False,
        }
    after_outlier = len(survivors_outlier)

    audit_flat = {
        "raw_count": raw_count,
        "kept": after_outlier,
        "dropped_low_quality": raw_count - after_low_quality,
        "dropped_apple_to_apples": after_low_quality - after_score,
        "dropped_stale": after_score - after_stale,
        "dropped_outlier": after_stale - after_outlier,
    }
    audit_verbose = {
        "low_quality_drops": dict(lq_audit.get("dropped_reasons", {})),
        "outlier_audit": {
            "fence_lo": outlier_audit.get("fence_lo"),
            "fence_hi": outlier_audit.get("fence_hi"),
            "log_transform": outlier_audit.get("log_transform"),
            "own_in_outlier_zone": outlier_audit.get("own_in_outlier_zone", False),
            "skipped_reason": outlier_audit.get("skipped_reason"),
        },
    }
    return survivors_outlier, audit_flat, audit_verbose
