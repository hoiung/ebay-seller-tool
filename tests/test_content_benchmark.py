"""Unit tests for ebay.browse apple-to-apples scoring + ebay.content_benchmark (#13 Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ebay.browse import (
    drop_stale_competitors,
    filter_clean_competitors,
    score_apple_to_apple,
)
from ebay.content_benchmark import compute_content_benchmarks


def _own(**overrides) -> dict:
    """Build a representative own_listing dict (matches listing_to_dict shape)."""
    base = {
        "specifics": {
            "MPN": ["ST2000NX0253"],
            "Form Factor": ['2.5"'],
        },
        "condition_id": "3000",
        "condition_name": "Used",
        "photos": ["a.jpg", "b.jpg", "c.jpg"],
        "best_offer_enabled": False,
        "return_policy": {"period_days": 14, "returns_accepted": True},
    }
    base.update(overrides)
    return base


def _comp(
    *,
    title: str = 'ST2000NX0253 2.5" SAS HDD',
    condition: str = "Used",
    condition_id: str | None = "3000",
    age_days: int = 30,
    **extras,
) -> dict:
    """Build a comp_item dict with a recent creation date by default."""
    creation = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    base = {
        "title": title,
        "price": 35.00,
        "condition": condition,
        "condition_id": condition_id,
        "item_creation_date": creation,
        "image_url": "https://i.ebayimg.com/x.jpg",
        "additional_image_count": 4,
        "top_rated": True,
        "returns_accepted": True,
        "returns_within_days": 30,
        "seller_feedback_pct": "99.5",
        "seller_feedback_score": 1000,
    }
    base.update(extras)
    return base


# === score_apple_to_apple (Issue #14: 4 dims × 0.25 + Layer-2 deductions) ==


def test_score_perfect_match() -> None:
    """All 4 dims pass (MPN + FF + Cond equivalence + age <200d) → 1.0."""
    score = score_apple_to_apple(_own(), _comp())
    assert score == 1.0


def test_score_missing_mpn_dim() -> None:
    """MPN dim fails → -0.25 → 0.75."""
    score = score_apple_to_apple(_own(), _comp(title='2.5" SAS HDD generic'))
    assert score == 0.75


def test_score_bundle_keyword_no_score_impact() -> None:
    """Issue #14 Phase 1.6: bundle dim removed from scorer.

    'caddy' in title is now a Layer-1 hard reject (when own has caddy) handled
    by filter_low_quality_competitors. Score function ignores bundle keywords;
    a comp with all 4 structural dims passing scores 1.0 regardless of caddy
    text in the title.
    """
    score = score_apple_to_apple(_own(), _comp(title='ST2000NX0253 with caddy 2.5"'))
    assert score == 1.0


def test_score_form_factor_mismatch() -> None:
    """Comp is 3.5" not 2.5" → FF dim fails → 0.75."""
    score = score_apple_to_apple(_own(), _comp(title="ST2000NX0253 3.5 SAS"))
    assert score == 0.75


def test_score_condition_equivalence_class_match() -> None:
    """Issue #14 Phase 2.3: own=3000 (Used) and comp=2750 (Used-Excellent) → equivalent → +0.25."""
    score = score_apple_to_apple(_own(), _comp(condition="Used – Excellent", condition_id="2750"))
    assert score == 1.0


def test_score_condition_equivalence_class_strict_mismatch() -> None:
    """own=3000 (Used) vs comp=1000 (New) → NOT equivalent → -0.25 → 0.75."""
    score = score_apple_to_apple(_own(), _comp(condition="New", condition_id="1000"))
    assert score == 0.75


def test_score_stale_listing() -> None:
    """Comp listed 365 days ago → age dim fails → -0.25 → 0.75."""
    score = score_apple_to_apple(_own(), _comp(age_days=365))
    assert score == 0.75


def test_score_missing_creation_date_treats_as_unknown() -> None:
    """Issue #14 Phase 0.4: missing creation_date no longer rewards +0.2.

    Treat unknown as worst-case (no age contribution). Score drops 0.25.
    """
    comp = _comp()
    comp["item_creation_date"] = None
    score = score_apple_to_apple(_own(), comp)
    assert score == 0.75


def test_score_multiple_failures() -> None:
    """MPN miss + FF miss + cond mismatch (3000↔1000) + age ok = 0.25."""
    score = score_apple_to_apple(
        _own(),
        _comp(title="kit 3.5 generic bundle", condition="New", condition_id="1000"),
    )
    assert score == 0.25


def test_score_layer2_seller_feedback_pct_deduction() -> None:
    """Layer-2: seller_feedback_pct < 98.0 deducts 0.05."""
    score = score_apple_to_apple(_own(), _comp(seller_feedback_pct="95.0"))
    assert score == 0.95


def test_score_layer2_returns_not_accepted_deduction() -> None:
    """Layer-2: returns_accepted=False (explicit) deducts 0.05."""
    score = score_apple_to_apple(_own(), _comp(returns_accepted=False))
    assert score == 0.95


def test_score_layer2_top_rated_not_true_deduction() -> None:
    """Layer-2: top_rated is not True (None or False) deducts 0.05."""
    score = score_apple_to_apple(_own(), _comp(top_rated=False))
    assert score == 0.95


def test_score_layer2_combined_deductions() -> None:
    """Layer-2: 3 deductions stack (0.05 × 3 = 0.15) → 1.0 - 0.15 = 0.85."""
    score = score_apple_to_apple(
        _own(),
        _comp(seller_feedback_pct="95.0", returns_accepted=False, top_rated=False),
    )
    assert score == 0.85


# === filter_clean_competitors =====================================


def test_filter_clean_keeps_only_above_threshold() -> None:
    own = _own()
    comps = [
        _comp(),  # 1.0
        _comp(title="ST2000NX0253 with caddy 2.5"),  # 1.0 (bundle dim removed Phase 1.6)
        _comp(title="ST2000NX0253 3.5 SAS"),  # MPN+Cond+age, no FF → 0.75
        _comp(title="generic 3.5 SAS", condition="New", condition_id="1000"),  # 0.25
    ]
    kept = filter_clean_competitors(own, comps, threshold=0.6)
    # 1.0, 1.0, 0.75 kept; 0.25 dropped.
    assert len(kept) == 3


def test_filter_clean_threshold_strict() -> None:
    """At threshold==1.0 only perfect matches survive."""
    own = _own()
    comps = [
        _comp(),  # 1.0
        _comp(title="generic kit bundle"),  # FF + Cond + Age, no MPN → 0.75
        _comp(condition="New", condition_id="1000"),  # cond mismatch → 0.75
    ]
    kept = filter_clean_competitors(own, comps, threshold=1.0)
    assert len(kept) == 1


# === drop_stale_competitors =====================================


def test_drop_stale_removes_oldest_pct() -> None:
    """10 comps, drop_pct=10 → 1 oldest dropped, 9 retained."""
    comps = [_comp(age_days=age) for age in [10, 20, 30, 40, 50, 60, 70, 80, 90, 365]]
    kept = drop_stale_competitors(comps, drop_pct=10.0)
    assert len(kept) == 9
    # The 365-day stale one should be gone; max age in kept ≤ 90.
    ages = [
        (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(
                c["item_creation_date"].replace("Z", "+00:00")
                if c["item_creation_date"].endswith("Z")
                else c["item_creation_date"]
            )
        ).days
        for c in kept
    ]
    assert max(ages) <= 91


def test_drop_stale_no_op_under_min_size() -> None:
    """1 comp, drop_pct=10 → drop_count = floor(0.1) = 0, kept unchanged."""
    comps = [_comp(age_days=300)]
    kept = drop_stale_competitors(comps, drop_pct=10.0)
    assert len(kept) == 1


def test_drop_stale_undated_retained() -> None:
    """Listings without item_creation_date are kept regardless."""
    c1 = _comp(age_days=30)
    c2 = _comp()
    c2["item_creation_date"] = None
    kept = drop_stale_competitors([c1, c2], drop_pct=50.0)
    # c2 (undated) always kept; c1 may be dropped depending on rounding —
    # 1 item dated, drop_pct=50 → drop_count = 0 → both kept.
    assert len(kept) == 2
    assert c2 in kept


# === content benchmarks ===========================================


def test_content_benchmarks_all_ok() -> None:
    own = _own()
    own["photos"] = ["a", "b", "c", "d", "e"]  # 5 photos > p25 of comps (assuming p25<5)
    own["best_offer_enabled"] = True
    own["return_policy"] = {"period_days": 30, "returns_accepted": True}
    comps = [
        _comp(additional_image_count=2),  # total 3
        _comp(additional_image_count=3),  # total 4
        _comp(additional_image_count=4, best_offer_enabled=True),  # total 5
        _comp(additional_image_count=4, best_offer_enabled=True),
        _comp(additional_image_count=5, best_offer_enabled=False),
    ]
    result = compute_content_benchmarks(own, comps, own_top_rated=True)
    assert result["photo_count"]["verdict"] == "ok"
    # comps p25 of [3,4,5,5,6] → ~3.5; own=5 → ok
    assert result["best_offer_posture"]["verdict"] == "ok"  # own enabled
    assert result["top_rated_seller_gap"]["verdict"] == "ok"
    assert result["returns_policy_generosity"]["verdict"] == "ok"


def test_content_benchmark_photo_count_flagged() -> None:
    """Own photos < comp p25 → flagged."""
    own = _own()
    own["photos"] = ["a"]  # 1 photo
    comps = [_comp(additional_image_count=10) for _ in range(5)]  # total 11 each
    result = compute_content_benchmarks(own, comps)
    assert result["photo_count"]["verdict"] == "flagged"
    assert result["photo_count"]["own_value"] == 1
    assert "Add 2-3 angles" in result["photo_count"]["action_if_flagged"]


def test_content_benchmark_best_offer_flagged_when_comps_majority() -> None:
    """Comp BO% > 50 AND own=False → flagged."""
    own = _own()
    own["best_offer_enabled"] = False
    comps = [
        _comp(best_offer_enabled=True),
        _comp(best_offer_enabled=True),
        _comp(best_offer_enabled=True),
        _comp(best_offer_enabled=False),
    ]  # 75% BO
    result = compute_content_benchmarks(own, comps)
    assert result["best_offer_posture"]["verdict"] == "flagged"
    assert result["best_offer_posture"]["comp_pct"] == 75.0


def test_content_benchmark_top_rated_flagged() -> None:
    """Comp >40% top-rated AND own_top_rated=False → flagged."""
    own = _own()
    comps = [_comp(top_rated=True) for _ in range(5)]  # 100% top-rated
    result = compute_content_benchmarks(own, comps, own_top_rated=False)
    assert result["top_rated_seller_gap"]["verdict"] == "flagged"
    assert result["top_rated_seller_gap"]["comp_pct"] == 100.0


def test_content_benchmark_returns_policy_flagged() -> None:
    """Own returns_within_days < comp p50 → flagged."""
    own = _own()
    own["return_policy"] = {"period_days": 14, "returns_accepted": True}
    comps = [_comp(returns_within_days=30) for _ in range(5)]  # all 30d
    result = compute_content_benchmarks(own, comps)
    assert result["returns_policy_generosity"]["verdict"] == "flagged"
    assert result["returns_policy_generosity"]["own_value"] == 14
    assert result["returns_policy_generosity"]["comp_p50"] == 30


def test_content_benchmark_handles_all_none_values() -> None:
    """Empty/None comp data should yield None aggregates, no crash."""
    own = _own()
    comps: list[dict] = []
    result = compute_content_benchmarks(own, comps)
    assert result["photo_count"]["comp_p25"] is None
    assert result["best_offer_posture"]["comp_pct"] is None
    assert result["top_rated_seller_gap"]["comp_pct"] is None
    assert result["returns_policy_generosity"]["comp_p50"] is None
    # All verdicts should be ok (no flagged when no comps to compare against)
    assert result["photo_count"]["verdict"] == "ok"
