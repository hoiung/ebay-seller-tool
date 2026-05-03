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
Issue #444 Part B added equivalence-class fetch widening at the orchestrator:
    `_sync_find_competitor_prices` reads `comp_filter.condition_equivalence`
    from YAML (same key already used by `score_apple_to_apple` Dim 3) and
    issues ONE Browse API call per equivalence-class member, dedupes by
    `item_id`, then runs the 3-layer pipeline. USED → 3000 + 2750 surfaces
    Used-Excellent listings the API previously hid behind single-ID truncation.

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


# Issue #14 Phase 2.4 + Issue #444 Part B — Browse search uses single conditionId per call,
# orchestrator loops over the equivalence class.
#
# Live curl verification 2026-04-25 against eBay Browse v1: pipe-separator
# syntax `conditionIds:{3000|2750}` is silently TRUNCATED by the API to
# `conditionIds:{3000}` (first ID only). Comma-separator returns unrelated
# results (1000/2010/3000 mix). Conclusion: Browse `conditionIds` accepts
# exactly ONE ID per filter string at the API layer.
#
# Equivalence-class widening is now IMPLEMENTED at the orchestrator layer
# (`_sync_find_competitor_prices`): when the YAML
# `comp_filter.condition_equivalence` block defines a multi-ID class for the
# requested condition (e.g. {"3000": ["3000", "2750"]} for USED), the
# orchestrator issues ONE Browse API call per class member via
# `_fetch_one_condition_id`, merges results, dedupes by `item_id`, and
# recomputes counters from the deduped pool. Score-side equivalence in
# `score_apple_to_apple` Dim 3 remains as defence-in-depth fallback.
_BROWSE_CONDITION_FILTERS: dict[str, str] = {
    "NEW": "1000",
    "USED": "3000",
    "USED_EXCELLENT": "2750",
    "OPENED": "1500",
    "FOR_PARTS": "7000",
}


def _condition_id_for(condition: str) -> str:
    """Map condition string to eBay primary conditionId (single-ID per CALL).

    The API accepts only one conditionId per filter string. Equivalence-class
    widening (e.g. USED → 3000 + 2750) is the orchestrator's job — see
    `_sync_find_competitor_prices` and YAML `comp_filter.condition_equivalence`.
    See module-level note above for the live-curl evidence behind the single-ID rule.
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


def _fetch_one_condition_id(
    part_number: str,
    cond_id: str,
    location_country: str,
    limit: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    """One Browse API call for a single conditionId (Issue #444 Part B).

    Returns ``(listings, currencies_seen)`` only — counter accumulation
    (shipping_free / best_offer / promoted / by_condition) is the orchestrator's
    responsibility, computed from deduped merged listings to avoid double-counting
    items that surface in multiple equivalence-class calls.

    Per-listing parsing is preserved verbatim from the pre-refactor inline loop:
    own-seller skip, price-parse skip, NaN/Inf guard, condition_id capture,
    shipping_cost extraction. Listings with ``itemId is None`` are INCLUDED in
    the return — orchestrator dedupe handles the None-skip.

    The internal ``_promoted`` flag carries the (listingMarketplaceId AND PROMOTED-
    in-itemAffiliateWebUrl) detection forward so the orchestrator can recompute
    promoted_count from deduped listings without re-walking raw items.
    """
    # Stub #19 — set fieldgroups=ADDITIONAL_SELLER_DETAILS so seller.username +
    # feedbackScore + feedbackPercentage land in the response. Per D2 platform
    # change 2025-09-26 Browse API depreciates seller.username for US sellers
    # (renders as null); the EXTENDED group is the only way to retrieve seller
    # quality signals at all. UK/EU sellers continue to populate username.
    params = {
        "q": part_number,
        "filter": (
            f"buyingOptions:{{FIXED_PRICE}},"
            f"conditionIds:{{{cond_id}}},"
            f"itemLocationCountry:{{{location_country}}}"
        ),
        "limit": str(min(max(limit, 1), 200)),
        "fieldgroups": "ADDITIONAL_SELLER_DETAILS",
    }
    with get_browse_session() as client:
        response = client.get("/buy/browse/v1/item_summary/search", params=params)
    raise_for_ebay_error(response)
    payload = response.json()

    own = _own_seller_lower()
    raw_listings = payload.get("itemSummaries", []) or []

    listings: list[dict[str, Any]] = []
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

        shipping_cost = _first_shipping_cost(item)
        cond = item.get("condition", "UNKNOWN")
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
                # Issue #444 — internal flag for orchestrator promoted_count recompute.
                "_promoted": bool(item.get("listingMarketplaceId"))
                and "PROMOTED" in str(item.get("itemAffiliateWebUrl", "")),
            }
        )

    return listings, currencies_seen


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
    primary_cond_id = _condition_id_for(condition)

    # Issue #444 Part B — equivalence-class widening at the orchestrator layer.
    # YAML `comp_filter.condition_equivalence` already populated; same key is read
    # by `score_apple_to_apple` Dim 3 — single source of truth.
    # `or [primary_cond_id]` falls back on BOTH None (key absent) AND empty list
    # (defensive against `condition_equivalence['3000']: []`).
    equivalence_cfg = (
        _load_filter_config().get("comp_filter", {}).get("condition_equivalence", {}) or {}
    )
    cond_ids: list[str] = equivalence_cfg.get(primary_cond_id) or [primary_cond_id]

    seen_item_ids: set[str] = set()
    raw_count_per_condition_id: dict[str, int] = {}
    merged_listings: list[dict[str, Any]] = []
    currencies_seen: set[str] = set()

    for cid in cond_ids:
        per_call_listings, per_call_currencies = _fetch_one_condition_id(
            part_number, cid, location_country, limit
        )
        # Raw count BEFORE cross-call dedupe — includes None-itemId entries
        # (the dedupe handles None as a skip; raw count preserves the audit trail).
        raw_count_per_condition_id[cid] = len(per_call_listings)
        for listing in per_call_listings:
            iid = listing.get("item_id")
            if iid is None or iid in seen_item_ids:
                continue
            seen_item_ids.add(iid)
            merged_listings.append(listing)
        currencies_seen |= per_call_currencies

    if len(currencies_seen) > 1:
        raise ValueError(
            f"Browse API response mixed currencies {sorted(currencies_seen)}; "
            f"refusing to aggregate. Filter `itemLocationCountry` should prevent this."
        )
    currency = next(iter(currencies_seen)) if currencies_seen else "GBP"

    # Recompute counters from the DEDUPED pool in one pass — avoids double-counting
    # items that surfaced in multiple equivalence-class calls.
    listings: list[dict[str, Any]] = []
    prices: list[float] = []
    shipping_free_count = 0
    best_offer_count = 0
    promoted_count = 0
    by_condition: dict[str, int] = {}
    for listing in merged_listings:
        prices.append(listing["price"])
        if listing.get("shipping_cost") is not None and listing["shipping_cost"] == 0.0:
            shipping_free_count += 1
        if listing.get("best_offer_enabled"):
            best_offer_count += 1
        if listing.get("_promoted"):
            promoted_count += 1
        cond = listing.get("condition", "UNKNOWN")
        by_condition[cond] = by_condition.get(cond, 0) + 1
        # Strip the internal _promoted flag before exposing the listing dict.
        public_listing = {k: v for k, v in listing.items() if k != "_promoted"}
        listings.append(public_listing)

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
            empty["audit_flat"] = {
                "raw_count": 0,
                "kept": 0,
                "dropped_low_quality": 0,
                "dropped_apple_to_apples": 0,
                "dropped_stale": 0,
                "dropped_outlier": 0,
            }
            # Issue #444 Part B — surface per-condition raw counts even on empty pool
            # so callers can see which equivalence-class members returned zero.
            empty["audit_verbose"] = {"raw_count_per_condition_id": raw_count_per_condition_id}
            # Stub #20 — 3-verdict carve-out. Raw Browse returned zero items
            # = genuinely platform-niche (LONE_SUPPLIER), distinct from "filter
            # killed everything" (ALL_FILTERED). Different operator response.
            empty["verdict"] = "LONE_SUPPLIER"
            empty["recommended_action"] = "anchor_via_cogs_plus_target_margin"
            empty["fallback"] = {
                "rationale": (
                    "no market signal from Browse — fall back to cost-plus + "
                    "target margin OR own-history (get_sold_listings) for anchor"
                ),
                "suggested_inputs": ["floor_price_gbp", "get_sold_listings(MPN)"],
            }
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
    # Issue #444 Part B — propagate per-condition raw counts (BEFORE dedupe)
    # through audit_verbose so callers can compare 3000 vs 2750 populations.
    audit_verbose["raw_count_per_condition_id"] = raw_count_per_condition_id

    if not kept:
        # Stub #20 — ALL_FILTERED. raw>0 (we wouldn't reach here from the raw=0
        # branch above) but every comp dropped by Layer-1/2/3. Surfaces the raw
        # count so the operator can decide whether to relax the filter or
        # accept lone-supplier-like fallback.
        return {
            **raw_distribution,
            "count": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "max": None,
            "listings": [],
            "audit_flat": audit_flat,
            "audit_verbose": audit_verbose,
            "verdict": "ALL_FILTERED",
            "recommended_action": "review_filter_settings",
            "pre_filter_count": audit_flat.get("raw_count", 0),
        }

    kept_prices = sorted(c["price"] for c in kept)
    kept_n = len(kept_prices)
    out: dict[str, Any] = {
        **raw_distribution,
        "count": kept_n,
        "min": round(kept_prices[0], 2),
        "p25": round(kept_prices[max(0, kept_n // 4)], 2),
        "median": round(statistics.median(kept_prices), 2),
        "p75": round(kept_prices[min(kept_n - 1, (3 * kept_n) // 4)], 2),
        "max": round(kept_prices[-1], 2),
        "listings": kept,
        "audit_flat": audit_flat,
        "audit_verbose": audit_verbose,
    }
    # Stub #20 — THIN_POOL. 1<=kept<=3 sample is too small to anchor pricing
    # confidently; surface the verdict + low-confidence flag so consumers can
    # weight stats accordingly. > 3 = no verdict surfaced (normal pool).
    if 1 <= kept_n <= 3:
        out["verdict"] = "THIN_POOL"
        out["recommended_action"] = "use_with_low_confidence"
        out["confidence"] = "low"
    return out


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
    drop) before returning. The result dict gains ``audit`` (flat 7-key — Stub
    #19 added ``concentration``) and ``audit_verbose`` (per-reason histogram),
    and ``count``/``min``/``p25``/``median``/``p75``/``max``/``listings``
    reflect the kept comps. Stub #20 surfaces a 3-verdict carve-out:

      * ``LONE_SUPPLIER`` — raw Browse returned zero items (genuinely platform-
        niche); pairs with ``recommended_action: anchor_via_cogs_plus_target_margin``
      * ``ALL_FILTERED`` — raw>0 but every comp dropped by the pipeline; pairs
        with ``recommended_action: review_filter_settings`` + ``pre_filter_count``
      * ``THIN_POOL`` — 1<=kept<=3 sample; pairs with ``recommended_action:
        use_with_low_confidence`` + ``confidence: low``

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


def _own_mpns(own_listing: dict[str, Any] | None) -> list[str]:
    """Extract own MPN list (uppercased, stripped) from own_listing.specifics.

    Returns [] when own_listing is None, has no specifics, or no MPN key.
    Multi-MPN listings supported (eBay item_specifics MPN can be a list).
    """
    if not own_listing:
        return []
    specifics = own_listing.get("specifics") or {}
    mpns = specifics.get("MPN") or []
    if isinstance(mpns, str):
        mpns = [mpns]
    return [str(m).upper().strip() for m in mpns if str(m).strip()]


def _comp_title_has_own_or_sibling_mpn(
    comp_title_upper: str,
    own_mpns: list[str],
    sibling_allowlist: dict[str, list[str]],
) -> bool:
    """Stub #18 — comp passes mpn_mismatch gate if its title contains:
        (a) any own.MPN, OR
        (b) any sibling MPN from the allowlist for any own.MPN.

    The allowlist is keyed by own.MPN → list of accepted sibling MPNs. Designed
    to be authored bidirectionally for symmetry (add the same pair on both
    sides), but this function is one-directional per call.
    """
    if any(mpn and mpn in comp_title_upper for mpn in own_mpns):
        return True
    if not sibling_allowlist:
        return False
    siblings: set[str] = set()
    for own_mpn in own_mpns:
        for s in sibling_allowlist.get(own_mpn, []) or []:
            siblings.add(str(s).upper().strip())
    return any(s and s in comp_title_upper for s in siblings)


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
    # Stub #18 — distinct-SKU verification. Extract own's MPN list; gate the
    # mpn_mismatch hard-reject on `len(own_mpns) >= 1` (skip when no MPN in
    # specifics so we don't over-restrict listings without a known part number).
    own_mpns = _own_mpns(own_listing)
    sibling_allowlist = cfg.get("comp_filter", {}).get("sibling_allowlist", {}) if own_mpns else {}

    survivors: list[dict[str, Any]] = []
    drops: dict[str, int] = {}

    for comp in comp_listings:
        title = str(comp.get("title") or "")
        title_lower = title.lower()
        title_upper = title.upper()

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

        # Stub #18 — mpn_mismatch hard-reject. Browse API has no native MPN
        # parameter, so we filter client-side: if own MPN(s) are known and
        # NONE appear in the comp title, the comp is for a different SKU
        # (e.g. ST2000NX0253 vs ST2000NX0403 — same family, different firmware).
        # Sibling-allowlist provides escape hatch for legitimate cross-MPN pairs.
        if own_mpns and not _comp_title_has_own_or_sibling_mpn(
            title_upper, own_mpns, sibling_allowlist
        ):
            drops["mpn_mismatch"] = drops.get("mpn_mismatch", 0) + 1
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


def compute_seller_concentration(comps: list[dict[str, Any]]) -> dict[str, Any]:
    """Stub #19 — compute seller-pool concentration stats from a comp list.

    Returns a 4-key dict:
      top_seller_pct      — share of comps held by the most-listed seller, 0..1
      distinct_sellers    — count of unique seller identities
      herfindahl          — HHI (sum of squared shares), 0..1
      confidence          — 'normal' | 'low' | 'insufficient_pool'

    Identity resolution: prefer ``comp["seller"]`` (Browse username); when null
    (US sellers post 2025-09-26 deprecation) fall back to a quasi-identity
    composed from feedback_score + feedback_pct so two comps from "same
    feedback profile" are treated as one seller. Imperfect but better than
    treating every null username as a unique seller.

    Thin-pool short-circuit: when distinct_sellers == 0 OR len(comps) < the
    configured ``concentration.min_pool_size`` (default 4), all numeric stats
    return None and confidence='insufficient_pool' — never flag concentration
    on under-powered samples.

    Boundary alignment with the THIN_POOL verdict in _sync_find_competitor_prices:
    THIN_POOL fires when 1<=kept_n<=3 (sample exists but is thin); concentration
    short-circuits when n<4. Both treat n>=4 as "enough to compute" — at n=3
    a consumer reads `verdict: THIN_POOL` AND `concentration.confidence:
    insufficient_pool`, which agree. At n=4, the verdict has no THIN_POOL flag
    and concentration is computed — matching the "normal pool" cutoff.
    """
    from collections import Counter

    cfg = _load_filter_config()
    conc_cfg = cfg.get("comp_filter", {}).get("concentration", {}) or {}
    threshold_pct = float(conc_cfg.get("threshold_pct", 0.40))
    min_pool_size = int(conc_cfg.get("min_pool_size", 4))

    def _quasi_id(c: dict[str, Any]) -> str:
        seller = c.get("seller")
        if seller:
            return f"seller:{str(seller).lower()}"
        # Fallback: feedback profile (handles null username cases)
        fb_score = c.get("seller_feedback_score") or "x"
        fb_pct = c.get("seller_feedback_pct") or "x"
        return f"fb:{fb_score}:{fb_pct}"

    distinct = len({_quasi_id(c) for c in comps})
    if distinct == 0 or len(comps) < min_pool_size:
        return {
            "top_seller_pct": None,
            "distinct_sellers": distinct,
            "herfindahl": None,
            "confidence": "insufficient_pool",
        }

    counts = Counter(_quasi_id(c) for c in comps)
    n = len(comps)
    top_pct = counts.most_common(1)[0][1] / n
    herfindahl = sum((c / n) ** 2 for c in counts.values())
    return {
        "top_seller_pct": round(top_pct, 4),
        "distinct_sellers": distinct,
        "herfindahl": round(herfindahl, 4),
        "confidence": "low" if top_pct > threshold_pct else "normal",
    }


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

    # Stub #19 — seller-pool concentration on the FINAL kept set (post-Layer-3).
    # Computing on survivors_outlier rather than raw_listings means the metric
    # reflects the comp pool the operator will actually see in pricing decisions,
    # not the raw Browse response (which includes everything we filter out).
    concentration = compute_seller_concentration(survivors_outlier)

    audit_flat = {
        "raw_count": raw_count,
        "kept": after_outlier,
        "dropped_low_quality": raw_count - after_low_quality,
        "dropped_apple_to_apples": after_low_quality - after_score,
        "dropped_stale": after_score - after_stale,
        "dropped_outlier": after_stale - after_outlier,
        "concentration": concentration,
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
