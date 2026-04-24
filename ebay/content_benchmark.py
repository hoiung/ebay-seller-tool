"""
Content benchmarks (Issue #13 Phase 2.2).

Compares own-listing content posture against clean (apple-to-apples) competitor
listings across 4 actionable dimensions. Each benchmark returns a verdict +
own/comp values + a recommended action so the weekly sweep can drive
fix lists without manual computation.

Per round-2 F-D: `seller_feedback_floor` is intentionally NOT included —
seller feedback is cumulative-historical and can't be moved inside the
weekly review window, so it's diagnostic but not actionable here.
"""

from __future__ import annotations

import statistics
from typing import Any


def _safe_p(values: list[float], q: float) -> float | None:
    """Return the q-quantile of values (q in [0,1]). None on empty list."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    # statistics.quantiles requires len >= 2
    quantiles = statistics.quantiles(sorted_vals, n=4, method="inclusive")
    # quantiles returns [p25, p50, p75]; pick the closest match.
    if q == 0.25:
        return quantiles[0]
    if q == 0.50:
        return quantiles[1]
    if q == 0.75:
        return quantiles[2]
    raise ValueError(f"_safe_p only supports q in {{0.25, 0.50, 0.75}}, got {q}")


def _photo_count_benchmark(
    own_photo_count: int,
    clean_comps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Photo count vs comp p25 — flagged when own < p25."""
    comp_counts: list[float] = []
    for c in clean_comps:
        # Browse exposes ONLY primary image_url + additional_image_count.
        # Total photos = 1 (primary) + additional, when image_url present.
        if c.get("image_url"):
            comp_counts.append(1 + (c.get("additional_image_count") or 0))
    p25 = _safe_p(comp_counts, 0.25)
    flagged = p25 is not None and own_photo_count < p25
    return {
        "verdict": "flagged" if flagged else "ok",
        "own_value": own_photo_count,
        "comp_p25": p25,
        "comp_n": len(comp_counts),
        "action_if_flagged": "Add 2-3 angles (label, ports, packaging) to lift photo count above p25",
    }


def _best_offer_benchmark(
    own_best_offer_enabled: bool | None,
    clean_comps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Best Offer posture — flagged when comp BO% > 50% AND own is NOT enabled.

    Browse API exposes `bestOfferEnabled` per listing. We aggregate the % across
    clean comps; if buyers commonly negotiate (>50% comps offer it) we recommend
    enabling it for own listing too.
    """
    bo_flags: list[bool] = []
    for c in clean_comps:
        # comp_item from listings[] doesn't surface bestOfferEnabled directly,
        # but the source dict at sync_find time records it. We check for the
        # field; if absent, omit from the count rather than assume False.
        if "best_offer_enabled" in c:
            bo_flags.append(bool(c["best_offer_enabled"]))
    pct = (100.0 * sum(bo_flags) / len(bo_flags)) if bo_flags else None
    flagged = (
        pct is not None
        and pct > 50.0
        and not bool(own_best_offer_enabled)
    )
    return {
        "verdict": "flagged" if flagged else "ok",
        "own_value": own_best_offer_enabled,
        "comp_pct": round(pct, 1) if pct is not None else None,
        "comp_n": len(bo_flags),
        "action_if_flagged": "Enable Best Offer — comp competitors are >50% offering negotiation",
    }


def _top_rated_benchmark(
    own_top_rated: bool,
    clean_comps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Top-Rated seller gap — investment area when comp >40% top-rated AND own not."""
    flags: list[bool] = []
    for c in clean_comps:
        v = c.get("top_rated")
        if v is not None:
            flags.append(bool(v))
    pct = (100.0 * sum(flags) / len(flags)) if flags else None
    flagged = pct is not None and pct > 40.0 and not own_top_rated
    return {
        "verdict": "flagged" if flagged else "ok",
        "own_value": own_top_rated,
        "comp_pct": round(pct, 1) if pct is not None else None,
        "comp_n": len(flags),
        "action_if_flagged": (
            "Investment area — improve dispatch speed + return-window resolution to qualify "
            "for Top-Rated Seller (>40% of comps already top-rated)"
        ),
    }


def _returns_policy_benchmark(
    own_returns_within_days: int | None,
    clean_comps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Returns-policy generosity vs comp p50 — flagged when own < p50."""
    durations: list[float] = []
    for c in clean_comps:
        d = c.get("returns_within_days")
        if d is not None:
            try:
                durations.append(float(d))
            except (TypeError, ValueError):
                continue
    p50 = _safe_p(durations, 0.50)
    flagged = (
        own_returns_within_days is not None
        and p50 is not None
        and own_returns_within_days < p50
    )
    return {
        "verdict": "flagged" if flagged else "ok",
        "own_value": own_returns_within_days,
        "comp_p50": p50,
        "comp_n": len(durations),
        "action_if_flagged": (
            f"Match the market median ({p50}d) — lengthening returns-within window "
            f"signals confidence and tracks Top-Rated criteria"
        ) if p50 is not None else None,
    }


def compute_content_benchmarks(
    own_listing: dict[str, Any],
    clean_comps: list[dict[str, Any]],
    own_top_rated: bool = False,
) -> dict[str, Any]:
    """Compute the 4 actionable content benchmarks for one own-listing vs clean comps.

    Args:
        own_listing: dict from listing_to_dict() — must have `photos`,
            `best_offer_enabled`, `return_policy.period_days`.
        clean_comps: list of comp_item dicts AFTER apple-to-apples filtering.
            Each must surface image_url / additional_image_count / top_rated /
            returns_within_days where available.
        own_top_rated: bool — whether OUR seller account is Top Rated. Browse
            doesn't expose this on own listings via Trading-API GetItem; pass
            from the orchestrator (e.g. derived from store account type).

    Returns:
        dict with keys: photo_count, best_offer_posture, top_rated_seller_gap,
        returns_policy_generosity. Each value is a benchmark dict.
    """
    # Photo count: own has the photos[] list from listing_to_dict.
    own_photos = len(own_listing.get("photos") or [])

    # Best Offer: own's listing_to_dict surfaces best_offer_enabled bool.
    own_bo = own_listing.get("best_offer_enabled")

    # Returns: own's listing_to_dict.return_policy.period_days.
    own_rp = (own_listing.get("return_policy") or {}).get("period_days")

    return {
        "photo_count": _photo_count_benchmark(own_photos, clean_comps),
        "best_offer_posture": _best_offer_benchmark(own_bo, clean_comps),
        "top_rated_seller_gap": _top_rated_benchmark(own_top_rated, clean_comps),
        "returns_policy_generosity": _returns_policy_benchmark(own_rp, clean_comps),
    }
