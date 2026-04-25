"""Issue #14 — apple-to-apples competitor filter quality tests.

Covers:
  Phase 1: filter_low_quality_competitors — image binary + regex categories
           + caddy/series-name own-aware hard rejects
  Phase 1.6: bundle keyword removal from scorer (regression)
  Phase 2: condition_id capture + numeric equivalence-class scoring
  Phase 3: caddy + series-name structural matching
  Phase 4: drop_price_outliers — IQR with 3 guards (min_pool_size,
           max_drop_frac, own_live_price anchor)
  Phase 5: run_comp_filter_pipeline aggregator + audit dict + JSON-serialisability
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from ebay.browse import (
    _compiled_hard_reject_patterns,
    _condition_id_for,
    drop_price_outliers,
    filter_low_quality_competitors,
    reset_filter_cache,
    run_comp_filter_pipeline,
    score_apple_to_apple,
)


def _own(**overrides) -> dict:
    base = {
        "title": "Seagate ST2000NX0253 2TB Enterprise Capacity 2.5 SAS HDD",
        "specifics": {
            "MPN": ["ST2000NX0253"],
            "Form Factor": ['2.5"'],
        },
        "condition_id": "3000",
        "condition_name": "Used",
    }
    base.update(overrides)
    return base


def _comp(**overrides) -> dict:
    creation = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    base = {
        "item_id": "v1|c1",
        "title": "ST2000NX0253 2.5 SAS Enterprise Capacity HDD",
        "price": 35.00,
        "condition": "Used",
        "condition_id": "3000",
        "item_creation_date": creation,
        "image_url": "https://i.ebayimg.com/x.jpg",
        "additional_image_count": 4,
        "seller_feedback_pct": "99.5",
        "seller_feedback_score": 1000,
        "top_rated": True,
        "returns_accepted": True,
        "returns_within_days": 30,
        "best_offer_enabled": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Phase 1.5 — Layer-1 hard pre-rejection
# ---------------------------------------------------------------------------


def test_image_binary_zero_image_dropped() -> None:
    """1.5.1 — image_url=None + additional_image_count=0 → image_zero drop."""
    comps = [_comp(image_url=None, additional_image_count=0)]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["image_zero"] == 1


def test_image_binary_intermittent_omission_kept() -> None:
    """1.5.1 — image_url=None but additional_image_count=3 → kept (Browse intermittent omission)."""
    comps = [_comp(image_url=None, additional_image_count=3)]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert len(survivors) == 1
    assert audit["dropped_reasons"] == {}


def test_regex_broken_or_parts() -> None:
    """1.5.2 — Phase 1.3 broken_or_parts regex hard-rejects."""
    titles = [
        "ST2000NX0253 for parts",
        "ST2000NX0253 spares or repair",
        "ST2000NX0253 untested faulty",
        "ST2000NX0253 not working",
    ]
    comps = [_comp(item_id=str(i), title=t) for i, t in enumerate(titles)]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["broken_or_parts"] == 4


def test_regex_external_or_wrong_form_factor() -> None:
    """1.5.2 — external/USB/portable enclosures hard-rejected."""
    titles = [
        "ST2000NX0253 external hard drive USB 3.0",
        "Backup Plus portable HDD 2TB",
        "drive enclosure caddy",
    ]
    comps = [_comp(item_id=str(i), title=t) for i, t in enumerate(titles)]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["external_or_wrong_form_factor"] == 3


def test_regex_wrong_category() -> None:
    """1.5.2 — RAID controllers / caddies-only / brackets hard-rejected."""
    titles = [
        "Dell PERC H730 RAID controller card",
        "HPE caddy only no drive",
        "Drive sled bracket only",
    ]
    comps = [_comp(item_id=str(i), title=t) for i, t in enumerate(titles)]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["wrong_category"] == 3


def test_regex_bundle() -> None:
    """1.5.2 — bundle / lot / pack regex hard-rejects."""
    titles = [
        "5x HDD lot",
        "Lot of 10 drives",
        "10 pack of HDDs",
        "joblot of drives",
        "set of 5 drives",
    ]
    comps = [_comp(item_id=str(i), title=t) for i, t in enumerate(titles)]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["bundle"] == 5


def test_lot_space_padding_bug_regression() -> None:
    """1.5.3 — Phase 0.2 fix: word-bounded lot regex catches leading/trailing 'Lot'."""
    titles = [
        "5x HDD Lot",  # trailing Lot, no trailing space (old `" lot "` missed)
        "Lot of 10 drives",  # leading Lot, no leading space
    ]
    comps = [_comp(item_id=str(i), title=t) for i, t in enumerate(titles)]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["bundle"] == 2


def test_kit_false_positive_avoidance() -> None:
    """1.5.4 — single-drive listings with 'caddy kit' / 'mounting kit' etc. NOT rejected.

    Old _BUNDLE_KEYWORDS treated bare 'kit' as bundle marker. New bundle regex
    requires explicit count tokens — 'caddy kit' on a single-drive listing
    survives Layer-1.
    """
    survivors, audit = filter_low_quality_competitors(
        [_comp(title="WD 2TB drive with caddy kit")],
        own_listing=None,  # disable own-aware checks
    )
    assert len(survivors) == 1
    assert audit["dropped_reasons"] == {}


def test_audit_dict_shape() -> None:
    """1.5.5 — audit dict has raw / kept / dropped_reasons keys with correct counts."""
    comps = [
        _comp(item_id="1"),  # kept
        _comp(item_id="2", title="HDD for parts"),  # broken_or_parts
        _comp(item_id="3", image_url=None, additional_image_count=0),  # image_zero
    ]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=_own())
    assert audit["raw"] == 3
    assert audit["kept"] == 1
    assert audit["dropped_reasons"]["broken_or_parts"] == 1
    assert audit["dropped_reasons"]["image_zero"] == 1


def test_regex_pattern_cache_identity() -> None:
    """1.3.1 — compiled regex objects are cached by identity (no re-compile per call)."""
    a = _compiled_hard_reject_patterns()
    b = _compiled_hard_reject_patterns()
    assert a is b  # identity check — same dict object across calls
    # Check at least one pattern object identity within the dict.
    if a.get("bundle"):
        assert a["bundle"][0] is b["bundle"][0]


def test_filter_perf_bench_100_comps_under_50ms() -> None:
    """1.3.1 — 100-comp pool through filter must complete in <50ms (cache hit)."""
    comps = [_comp(item_id=str(i), title=f"ST2000NX0253 listing {i}") for i in range(100)]
    # Warm the cache.
    filter_low_quality_competitors(comps, own_listing=_own())
    start = time.perf_counter()
    filter_low_quality_competitors(comps, own_listing=_own())
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms < 50.0, f"Filter took {elapsed_ms:.1f}ms — exceeds 50ms ceiling"


# ---------------------------------------------------------------------------
# Phase 2.5 — condition_id numeric equivalence
# ---------------------------------------------------------------------------


def test_condition_id_for_used_widens_to_pipe_separated() -> None:
    """2.5.5 — _condition_id_for('USED') returns equivalence class '3000|2750'."""
    assert _condition_id_for("USED") == "3000|2750"
    assert _condition_id_for("USED_EXCELLENT") == "2750|3000"
    assert _condition_id_for("OPENED") == "1500|1000"
    assert _condition_id_for("NEW") == "1000"
    assert _condition_id_for("FOR_PARTS") == "7000"


def test_condition_equivalence_used_excellent_match() -> None:
    """2.5.2 — own=3000 + comp=2750 scores +0.25 (numeric equivalence)."""
    score = score_apple_to_apple(_own(), _comp(condition="Used – Excellent", condition_id="2750"))
    assert score == 1.0


def test_condition_equivalence_strict_class_no_false_match() -> None:
    """2.5.3 — own=1000 (New) + comp=2750 → no equivalence → no Dim-3 contribution."""
    score = score_apple_to_apple(
        _own(condition_id="1000", condition_name="New"),
        _comp(condition="Used – Excellent", condition_id="2750"),
    )
    assert score == 0.75  # MPN+FF+Age, no Cond


def test_condition_equivalence_for_parts_excluded() -> None:
    """2.5.4 — 7000 (For parts) NEVER matches anything else."""
    score = score_apple_to_apple(
        _own(condition_id="3000"),
        _comp(condition="For parts", condition_id="7000"),
    )
    assert score == 0.75  # MPN+FF+Age


# ---------------------------------------------------------------------------
# Phase 3.3 — caddy + series-name structural matching
# ---------------------------------------------------------------------------


def test_caddy_match_own_with_caddy_comp_silent_kept() -> None:
    """3.3.1 — own has +Caddy, comp silent on caddy → kept (unknown, not penalised at L1)."""
    own = _own(title="ST2000NX0253 +Caddy 2.5 SAS")
    comps = [_comp(title="ST2000NX0253 2.5 SAS HDD")]  # no caddy/no-caddy mention
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert len(survivors) == 1
    assert audit["dropped_reasons"] == {}


def test_caddy_mismatch_own_with_caddy_comp_no_caddy_dropped() -> None:
    """3.3.2 — own has +Caddy + comp says 'no caddy' → caddy_mismatch hard reject."""
    own = _own(title="ST2000NX0253 +Caddy 2.5 SAS")
    comps = [_comp(title="ST2000NX0253 bare drive no caddy")]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["caddy_mismatch"] == 1


def test_caddy_own_no_caddy_state_irrelevant() -> None:
    """3.3.3 — own without caddy → comp's caddy state doesn't drive Layer-1 reject.

    Use generic own title (no series name) to isolate caddy logic from series logic.
    """
    own = _own(title="Generic 2TB SAS HDD ST2000NX0253")  # no +Caddy, no series
    comps = [_comp(title="ST2000NX0253 bare drive no caddy")]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert len(survivors) == 1
    assert audit["dropped_reasons"] == {}


def test_series_name_mismatch_exos_vs_enterprise_capacity() -> None:
    """3.3.4 — own='Enterprise Capacity' + comp='Exos' same MPN → series_mismatch hard reject."""
    own = _own(title="Seagate ST2000NX0253 2TB Enterprise Capacity 2.5 SAS")
    comps = [_comp(title="Seagate ST2000NX0253 Exos 2TB 2.5 SAS")]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert len(survivors) == 0
    assert audit["dropped_reasons"]["series_mismatch"] == 1


def test_series_name_match_same_series_pass() -> None:
    """3.3.5 — own='Enterprise Capacity' + comp='Enterprise Capacity' → pass."""
    own = _own(title="Seagate ST2000NX0253 2TB Enterprise Capacity 2.5 SAS")
    comps = [_comp(title="Seagate ST2000NX0253 Enterprise Capacity 2TB 2.5 SAS")]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert len(survivors) == 1
    assert audit["dropped_reasons"] == {}


def test_series_name_no_own_series_default_pass() -> None:
    """3.3.6 — own has no series name → comp's series doesn't drive Layer-1 reject."""
    own = _own(title="Generic 2TB SAS HDD ST2000NX0253")  # no series name
    comps = [_comp(title="Seagate Exos ST2000NX0253 2TB 2.5 SAS")]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert len(survivors) == 1
    assert audit["dropped_reasons"] == {}


def test_caddy_detection_features_specifics() -> None:
    """3.1 — own.specifics['Features'] contains 'Caddy' → own_has_caddy=True."""
    own = _own(
        specifics={
            "MPN": ["ST2000NX0253"],
            "Form Factor": ['2.5"'],
            "Features": ["Caddy", "Hot Swap"],
        }
    )
    comps = [_comp(title="ST2000NX0253 bare drive no caddy")]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert audit["dropped_reasons"]["caddy_mismatch"] == 1


def test_caddy_detection_has_caddy_runtime_arg() -> None:
    """3.1 — own.has_caddy=True (runtime arg) → caddy detection active."""
    own = _own(has_caddy=True)
    comps = [_comp(title="ST2000NX0253 without caddy")]
    survivors, audit = filter_low_quality_competitors(comps, own_listing=own)
    assert audit["dropped_reasons"]["caddy_mismatch"] == 1


# ---------------------------------------------------------------------------
# Phase 4.5 — drop_price_outliers (IQR with 3 guards)
# ---------------------------------------------------------------------------


def _price_only(prices: list[float]) -> list[dict]:
    return [{"item_id": str(i), "price": p, "title": f"item {i}"} for i, p in enumerate(prices)]


def test_drop_outlier_pool_a_drops_high_extreme() -> None:
    """4.5.1 — pool [12.99, 24.99, 599.99] + 4 mid-cluster: 599.99 dropped."""
    comps = _price_only([12.99, 19.99, 24.99, 25.00, 28.50, 32.00, 599.99])
    kept, audit = drop_price_outliers(comps, multiplier=1.5, log_transform=True, min_pool_size=6)
    kept_prices = sorted(c["price"] for c in kept)
    assert 599.99 not in kept_prices
    assert audit["dropped"] >= 1
    assert audit["fence_lo"] is not None and audit["fence_hi"] is not None


def test_drop_outlier_bimodal_neither_cluster_dropped() -> None:
    """4.5.2 — bimodal [£18-50] + [£150-260]: neither cluster dropped (out of scope for IQR)."""
    comps = _price_only([18, 22, 30, 38, 45, 50, 150, 180, 220, 260])
    kept, audit = drop_price_outliers(comps, multiplier=1.5, log_transform=True, min_pool_size=6)
    assert audit["dropped"] == 0


def test_drop_outlier_below_min_pool_size_skipped() -> None:
    """4.5.3 — pool of N=4 (below min_pool_size=6) → no dropping."""
    comps = _price_only([10, 20, 30, 1000])
    kept, audit = drop_price_outliers(comps, min_pool_size=6)
    assert len(kept) == 4
    assert audit["skipped_reason"] == "below_min_pool_size"


def test_drop_outlier_own_price_anchor_guard() -> None:
    """4.5.4 — own_live_price=£500 in £25-median pool: outliers NOT dropped, flagged in audit."""
    comps = _price_only([18, 22, 25, 30, 32, 599])
    kept, audit = drop_price_outliers(
        comps, min_pool_size=6, log_transform=True, own_live_price=500.0
    )
    assert audit["own_in_outlier_zone"] is True
    assert len(kept) == 6  # nothing dropped


def test_drop_outlier_max_drop_frac_cap() -> None:
    """4.5.5 — synthetic pool with many extreme values: never drops more than max_drop_frac."""
    # 10 items with 6 extremes — max_drop_frac=0.20 caps at 2.
    comps = _price_only([20, 22, 25, 28, 1000, 1100, 1200, 1300, 1400, 1500])
    kept, audit = drop_price_outliers(
        comps, multiplier=1.5, log_transform=True, min_pool_size=6, max_drop_frac=0.20
    )
    assert audit["dropped"] <= 2  # ≤ 20% of 10


def test_drop_outlier_method_none_pass_through() -> None:
    """method='none' → pass-through with skipped_reason='method_none'."""
    comps = _price_only([10, 20, 30, 1000])
    kept, audit = drop_price_outliers(comps, method="none")
    assert len(kept) == 4
    assert audit["skipped_reason"] == "method_none"


# ---------------------------------------------------------------------------
# Phase 5 — pipeline aggregator + audit dict + JSON-serialisability
# ---------------------------------------------------------------------------


def test_pipeline_audit_flat_six_keys() -> None:
    """5.1 — flat audit dict has exactly 6 user-facing keys."""
    comps = [_comp(item_id=str(i), title=f"ST2000NX0253 listing {i}") for i in range(8)]
    _, audit_flat, _ = run_comp_filter_pipeline(
        comps,
        own_listing=_own(),
        outlier_config={
            "enabled": True,
            "method": "iqr",
            "min_pool_size": 6,
            "max_drop_frac": 0.20,
            "multiplier": 1.5,
            "log_transform": True,
        },
    )
    assert set(audit_flat.keys()) == {
        "raw_count",
        "kept",
        "dropped_low_quality",
        "dropped_apple_to_apples",
        "dropped_stale",
        "dropped_outlier",
    }


def test_pipeline_audit_verbose_per_reason_counters() -> None:
    """5.1 — audit_verbose has nested per-reason counters."""
    comps = [
        _comp(item_id="1"),  # kept
        _comp(item_id="2", title="HDD for parts"),  # broken_or_parts
    ]
    _, _, audit_verbose = run_comp_filter_pipeline(comps, own_listing=_own())
    assert "low_quality_drops" in audit_verbose
    assert "outlier_audit" in audit_verbose
    assert audit_verbose["low_quality_drops"].get("broken_or_parts") == 1


def test_pipeline_audit_json_serialisable() -> None:
    """5.3.2 — audit dicts round-trip through json.dumps without TypeError.

    server.py:1687 does json.dumps(result) — non-serialisable values crash.
    No re.Pattern, no set, no numpy.float64 leaking through.
    """
    comps = [_comp(item_id=str(i), title=f"ST2000NX0253 listing {i}") for i in range(6)]
    _, audit_flat, audit_verbose = run_comp_filter_pipeline(
        comps,
        own_listing=_own(),
        outlier_config={
            "enabled": True,
            "method": "iqr",
            "min_pool_size": 6,
            "max_drop_frac": 0.20,
            "multiplier": 1.5,
            "log_transform": True,
        },
    )
    payload = {"audit": audit_flat, "audit_verbose": audit_verbose}
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload  # no precision loss, no missing keys


def test_pipeline_zero_comps_after_filter() -> None:
    """5.3.3 — pipeline returns empty kept + audit explains why."""
    comps = [_comp(item_id=str(i), title=f"HDD for parts spares {i}") for i in range(5)]
    kept, audit_flat, audit_verbose = run_comp_filter_pipeline(comps, own_listing=_own())
    assert len(kept) == 0
    assert audit_flat["kept"] == 0
    assert audit_flat["dropped_low_quality"] == 5
    assert audit_verbose["low_quality_drops"]["broken_or_parts"] == 5


# ---------------------------------------------------------------------------
# Cleanup — reset cache between test sessions to keep config-overrides clean.
# ---------------------------------------------------------------------------


def teardown_module(module) -> None:  # noqa: ANN001
    reset_filter_cache()
