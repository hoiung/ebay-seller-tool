"""Pure-function tests for ebay.analytics.compute_best_offer_thresholds.

History:
- Issue #4 G-NEW-1: introduced 0.88/0.72 pinned thresholds + floor*1.05 buffer.
- Issue #16: config-driven thresholds (0.925/0.75/round-down by default), schema
  validator (Fail Fast), unreachable-branch cleanup. Existing 0.88/0.72 tests
  retained with EXPLICIT kwargs so they pin the historical semantic regardless
  of what the active fees.yaml `best_offer:` block currently contains. New
  tests below cover the config-driven path + schema validator + intentional
  shift for analyse_listing-style callers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from ebay.analytics import (
    _round_down_to_pound,
    _validate_best_offer_config,
    compute_best_offer_thresholds,
)
from ebay.fees import reset_fees_cache


# ---------------------------------------------------------------------------
# Issue #4 G-NEW-1 baseline tests — pinned to 0.88/0.72 + round_down=False via
# explicit kwargs + `isolated_fees_config()` fixture (empty best_offer block
# so the function falls back to historical float-rounded semantics regardless
# of what active config/fees.yaml says).
# ---------------------------------------------------------------------------


def test_canonical_88_72_against_50_quid_listing(isolated_fees_config) -> None:
    """Floor £18, live £50 → auto_accept = max(18.90, 44.00) = 44.00; auto_decline = max(18, 36) = 36."""
    isolated_fees_config()  # no best_offer block → round_down OFF
    r = compute_best_offer_thresholds(
        floor_gbp=18.0, live_price_gbp=50.0, auto_accept_pct=0.88, auto_decline_pct=0.72
    )
    assert r["auto_accept_gbp"] == 44.0
    assert r["auto_decline_gbp"] == 36.0
    assert r["floor_gbp"] == 18.0
    assert "auto_accept = max(" in r["rationale"]


def test_floor_buffer_dominates_when_live_low(isolated_fees_config) -> None:
    """Live £20, floor £18 → 88% of live (£17.60) < floor*1.05 (£18.90); buffer wins."""
    isolated_fees_config()
    r = compute_best_offer_thresholds(
        floor_gbp=18.0, live_price_gbp=20.0, auto_accept_pct=0.88, auto_decline_pct=0.72
    )
    assert r["auto_accept_gbp"] == 18.9
    # auto_decline: max(18, 0.72*20=14.4) = 18 (floor wins)
    assert r["auto_decline_gbp"] == 18.0


def test_high_live_price_uses_pct_thresholds(isolated_fees_config) -> None:
    """Live £200, floor £18 → both percentages dominate floor."""
    isolated_fees_config()
    r = compute_best_offer_thresholds(
        floor_gbp=18.0, live_price_gbp=200.0, auto_accept_pct=0.88, auto_decline_pct=0.72
    )
    assert r["auto_accept_gbp"] == 176.0  # 0.88 * 200
    assert r["auto_decline_gbp"] == 144.0  # 0.72 * 200


def test_custom_pct_overrides_default(isolated_fees_config) -> None:
    isolated_fees_config()
    r = compute_best_offer_thresholds(
        floor_gbp=10.0,
        live_price_gbp=100.0,
        auto_accept_pct=0.95,
        auto_decline_pct=0.80,
    )
    assert r["auto_accept_gbp"] == 95.0
    assert r["auto_decline_gbp"] == 80.0


def test_zero_floor_works(isolated_fees_config) -> None:
    """Zero-cost baseline (config/fees.yaml default) — floor near zero is valid."""
    isolated_fees_config()
    r = compute_best_offer_thresholds(
        floor_gbp=0.0, live_price_gbp=50.0, auto_accept_pct=0.88, auto_decline_pct=0.72
    )
    # max(0*1.05=0, 0.88*50=44) = 44; max(0, 0.72*50=36) = 36
    assert r["auto_accept_gbp"] == 44.0
    assert r["auto_decline_gbp"] == 36.0


def test_rationale_mentions_both_components(isolated_fees_config) -> None:
    isolated_fees_config()
    r = compute_best_offer_thresholds(
        floor_gbp=18.0, live_price_gbp=50.0, auto_accept_pct=0.88, auto_decline_pct=0.72
    )
    rationale = r["rationale"]
    assert "auto_accept" in rationale
    assert "auto_decline" in rationale
    assert "floor" in rationale


def test_two_dp_rounding_preserved_when_round_down_off(isolated_fees_config) -> None:
    """When round_down_to_pound is False/absent, return shape stays float at 2 dp."""
    isolated_fees_config()  # no best_offer block → round_down OFF
    r = compute_best_offer_thresholds(
        floor_gbp=18.0, live_price_gbp=49.99, auto_accept_pct=0.88, auto_decline_pct=0.72
    )
    assert r["auto_accept_gbp"] == 43.99  # 0.88 * 49.99 = 43.9912 → round 2 dp
    assert isinstance(r["auto_accept_gbp"], float)


# ---------------------------------------------------------------------------
# Issue #16 AC1.4 — 6 NEW tests for config-driven path + intentional shift
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_fees_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a synthetic fees.yaml in tmp_path + point EBAY_FEES_CONFIG at it.

    Yields a callable `write(cfg_dict)` that drops the dict to disk and resets
    the lru_cache. Each test gets a fresh isolated config — no cross-test
    pollution from the active config/fees.yaml.
    """
    config_path = tmp_path / "fees.yaml"
    monkeypatch.setenv("EBAY_FEES_CONFIG", str(config_path))

    base = {
        "ebay_uk": {
            "fvf_rate": 0.1548,
            "per_order_fee_gbp": 0.40,
            "marketplace_id": "EBAY_GB",
            "site_id": 3,
        },
        "postage": {"outbound_gbp": 3.50, "return_gbp": 3.50},
        "packaging_gbp": 0.60,
        "time_cost": {
            "mode": "sunk",
            "sale_gbp": 0.0,
            "return_gbp": 0.0,
            "hourly_rate_gbp": 30.0,
        },
        "defaults": {"cogs_gbp": 0.0, "return_rate": 0.10, "target_margin": 0.15},
        "under_pricing": {
            "velocity_median_default": 0.1,
            "recommended_band_low_pct": 40,
            "recommended_band_high_pct": 55,
        },
        "outlier_rejection": {
            "enabled": True,
            "method": "iqr",
            "multiplier": 1.5,
            "log_transform": True,
            "min_pool_size": 6,
            "max_drop_frac": 0.20,
            "per_condition": False,
        },
    }

    def write(extra: dict[str, Any] | None = None) -> None:
        cfg = dict(base)
        if extra:
            cfg.update(extra)
        config_path.write_text(yaml.safe_dump(cfg))
        reset_fees_cache()

    yield write
    reset_fees_cache()


def test_compute_best_offer_thresholds_reads_config_when_args_omitted(
    isolated_fees_config,
) -> None:
    """Config block populated + caller omits pcts → returns 0.925/0.75 round-down."""
    isolated_fees_config(
        {
            "best_offer": {
                "auto_accept_pct": 0.925,
                "auto_decline_pct": 0.75,
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        }
    )
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)
    # 0.925 * 50 = 46.25 → floor → 46; 0.75 * 50 = 37.5 → floor → 37
    assert r["auto_accept_gbp"] == 46
    assert r["auto_decline_gbp"] == 37
    assert isinstance(r["auto_accept_gbp"], int)
    assert "round_down_to_pound applied" in r["rationale"]


def test_compute_best_offer_thresholds_kwargs_override_config(
    isolated_fees_config,
) -> None:
    """Config has 0.925/0.75 + caller passes 0.88 → kwargs win.

    Validates server.py:2268 + enable_best_offer_all.py:101 unchanged behaviour:
    those callers pass explicit `auto_accept_pct=...` and must keep that value.
    """
    isolated_fees_config(
        {
            "best_offer": {
                "auto_accept_pct": 0.925,
                "auto_decline_pct": 0.75,
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        }
    )
    r = compute_best_offer_thresholds(
        floor_gbp=18.0, live_price_gbp=50.0, auto_accept_pct=0.88, auto_decline_pct=0.72
    )
    # round_down still active from config → math.floor applied:
    # 0.88 * 50 = 44.0 → floor → 44; 0.72 * 50 = 36.0 → floor → 36
    assert r["auto_accept_gbp"] == 44
    assert r["auto_decline_gbp"] == 36


def test_round_down_to_pound_helper_floors_correctly() -> None:
    """Pure helper: math.floor(pct * live_price)."""
    assert _round_down_to_pound(0.925, 25.13) == 23
    assert _round_down_to_pound(0.95, 52.10) == 49
    assert _round_down_to_pound(0.925, 50.0) == 46
    assert _round_down_to_pound(0.75, 8.0) == 6


def test_compute_best_offer_thresholds_falls_back_to_historical_default_when_config_missing(
    isolated_fees_config,
) -> None:
    """No best_offer block in config + no kwargs → 0.88/0.72 (pre-#16 path)."""
    isolated_fees_config()  # base cfg only, no best_offer block
    r = compute_best_offer_thresholds(floor_gbp=18.0, live_price_gbp=50.0)
    # round_down OFF (absent block → False default) → return is float
    assert r["auto_accept_gbp"] == 44.0  # 0.88 * 50
    assert r["auto_decline_gbp"] == 36.0  # 0.72 * 50
    assert isinstance(r["auto_accept_gbp"], float)


def test_compute_best_offer_thresholds_returns_rationale_field_unchanged(
    isolated_fees_config,
) -> None:
    """Schema regression test: 4-key return dict including `rationale` (string).

    Pins the contract for `enable_best_offer_all.py:134` (`plan.get('rationale', '')`)
    and `server.py:2268` (`**thresholds` spread). Stage 3 L1.5 finding:
    early Stage 2 §2 AFTER block dropped this field via ellipsis; #16 AC1.3
    explicitly preserves it.
    """
    isolated_fees_config(
        {
            "best_offer": {
                "auto_accept_pct": 0.925,
                "auto_decline_pct": 0.75,
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        }
    )
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)
    assert set(r.keys()) == {"auto_accept_gbp", "auto_decline_gbp", "floor_gbp", "rationale"}
    assert isinstance(r["rationale"], str)
    assert len(r["rationale"]) > 0


def test_analyse_listing_caller_shifts_to_config_driven_intentional(
    isolated_fees_config,
) -> None:
    """server.py:1832 calls with ONLY floor_gbp + live_price_gbp (no pcts).

    Pre-#16 this hit default literals 0.88/0.72; post-#16 it picks up
    config-driven 0.925/0.75 (round-down). This test PINS that intentional
    behaviour shift so an accidental future revert (e.g. someone reverting
    the signature defaults) breaks the test loudly. Documents the alignment
    between analyse_listing's recommendations and the operator-locked thresholds.
    """
    isolated_fees_config(
        {
            "best_offer": {
                "auto_accept_pct": 0.925,
                "auto_decline_pct": 0.75,
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        }
    )
    # Mimics server.py:1832 call shape
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)
    assert r["auto_accept_gbp"] == 46  # 0.925 * 50 = 46.25 → floor → 46
    assert r["auto_decline_gbp"] == 37  # 0.75 * 50 = 37.5 → floor → 37


# ---------------------------------------------------------------------------
# Issue #16 AC1.5 — schema validator (Fail Fast)
# ---------------------------------------------------------------------------


def test_compute_best_offer_thresholds_rejects_typo_config_key(
    isolated_fees_config,
) -> None:
    """Typo `auto_acceptpct` (missing underscore) must raise ValueError, NOT silently
    use the 0.88 default. Fail Fast — Engineering Requirements line 23.
    """
    isolated_fees_config(
        {
            "best_offer": {
                "auto_acceptpct": 0.925,  # TYPO — missing underscore
                "auto_decline_pct": 0.75,
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        }
    )
    with pytest.raises(ValueError, match="missing keys"):
        compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)


def test_validate_best_offer_config_rejects_out_of_range_pct() -> None:
    """auto_accept_pct = 1.5 is out of range → ValueError."""
    with pytest.raises(ValueError, match="must be float in"):
        _validate_best_offer_config(
            {
                "auto_accept_pct": 1.5,
                "auto_decline_pct": 0.75,
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_rejects_invariant_violation() -> None:
    """auto_decline_pct >= auto_accept_pct violates the d < a < 1.0 invariant."""
    with pytest.raises(ValueError, match="invariant violated"):
        _validate_best_offer_config(
            {
                "auto_accept_pct": 0.75,
                "auto_decline_pct": 0.80,  # higher than accept — invariant violation
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_accepts_absent_block() -> None:
    """Empty dict (absent block) is the fallback path — must NOT raise."""
    _validate_best_offer_config({})  # no exception


def test_floor_buffer_pct_negative_raises() -> None:
    """Negative buffer is a programming error — raise loudly instead of silent
    unreachable branch (Stage 3 L1.5 dead-code cleanup)."""
    with pytest.raises(ValueError, match="floor_buffer_pct"):
        compute_best_offer_thresholds(
            floor_gbp=10.0,
            live_price_gbp=50.0,
            auto_accept_pct=0.88,
            auto_decline_pct=0.72,
            floor_buffer_pct=-0.5,
        )
