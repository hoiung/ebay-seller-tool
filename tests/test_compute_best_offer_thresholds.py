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
    """Floor £18, live £50 → auto_accept = max(18.90, 44.00) = 44.00;
    auto_decline = max(18, 36) = 36."""
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


_QTY_TIER_CONFIG: dict[str, Any] = {
    "best_offer": {
        "qty_tiers": {1: 0.95, 2: 0.925, "default": 0.90},
        "auto_decline_pct": 0.75,
        "round_down_to_pound": True,
    }
}


def test_compute_best_offer_thresholds_reads_config_when_args_omitted(
    isolated_fees_config,
) -> None:
    """Config block populated + caller omits pcts → reads qty_tiers[1] (95%)."""
    isolated_fees_config(_QTY_TIER_CONFIG)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)
    # qty=1 → 0.95 * 50 = 47.5 → floor → 47; 0.75 * 50 = 37.5 → floor → 37
    assert r["auto_accept_gbp"] == 47
    assert r["auto_decline_gbp"] == 37
    assert isinstance(r["auto_accept_gbp"], int)
    assert "round_down_to_pound applied" in r["rationale"]


def test_compute_best_offer_thresholds_kwargs_override_config(
    isolated_fees_config,
) -> None:
    """Config has qty_tiers + caller passes 0.88 → kwargs win.

    Pins the kwarg-override path: callers that pin a specific pct (tests,
    enable_best_offer_all.py) keep their explicit value regardless of the
    operator's qty-tier ladder.
    """
    isolated_fees_config(_QTY_TIER_CONFIG)
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
    and `server.py:2268` (`**thresholds` spread). Issue #30 keeps the return-shape
    contract — only the rationale TEXT changes to name the qty tier used.
    """
    isolated_fees_config(_QTY_TIER_CONFIG)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)
    assert set(r.keys()) == {"auto_accept_gbp", "auto_decline_gbp", "floor_gbp", "rationale"}
    assert isinstance(r["rationale"], str)
    assert len(r["rationale"]) > 0


def test_analyse_listing_caller_shifts_to_qty_tier_intentional(
    isolated_fees_config,
) -> None:
    """server.py:1832 calls with ONLY floor_gbp + live_price_gbp (qty defaulted).

    Pre-#16 this hit default literals 0.88/0.72; post-#16 it picked up flat
    0.925/0.75; post-#30 the default-qty=1 path picks up qty_tiers[1]=0.95.
    Pins this intentional behaviour shift so an accidental future revert
    breaks the test loudly.
    """
    isolated_fees_config(_QTY_TIER_CONFIG)
    # Mimics server.py:1832 call shape (qty defaulted to 1)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)
    assert r["auto_accept_gbp"] == 47  # 0.95 * 50 = 47.5 → floor → 47
    assert r["auto_decline_gbp"] == 37  # 0.75 * 50 = 37.5 → floor → 37


# ---------------------------------------------------------------------------
# Issue #30 AC1.5 — schema validator (Fail Fast on old + malformed schemas)
# ---------------------------------------------------------------------------


def test_validate_best_offer_config_rejects_old_schema_keys() -> None:
    """Old auto_accept_pct / counter_offer_pct keys → ValueError naming Issue #30
    migration. Fail Fast on stale config, never silent fallback to old defaults.
    """
    with pytest.raises(ValueError, match="old schema") as ei:
        _validate_best_offer_config(
            {
                "auto_accept_pct": 0.925,
                "auto_decline_pct": 0.75,
                "counter_offer_pct": 0.95,
                "round_down_to_pound": True,
            }
        )
    assert "Issue #30" in str(ei.value)


def test_validate_best_offer_config_rejects_missing_default_tier() -> None:
    """qty_tiers MUST contain 'default' key (catch-all for qty values not listed)."""
    with pytest.raises(ValueError, match="must contain 'default'"):
        _validate_best_offer_config(
            {
                "qty_tiers": {1: 0.95, 2: 0.925},  # no 'default' key
                "auto_decline_pct": 0.75,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_rejects_str_numeric_qty_key() -> None:
    """Numeric qty_tiers keys must be int (YAML deserialises plain ints as int).
    A str-typed numeric like '1' would silently fail to match `quantity == 1`
    dispatch lookup, so reject loudly.
    """
    with pytest.raises(ValueError, match="numeric keys must be int"):
        _validate_best_offer_config(
            {
                "qty_tiers": {"1": 0.95, "default": 0.90},  # str numeric key
                "auto_decline_pct": 0.75,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_rejects_pct_below_decline_floor() -> None:
    """qty_tier values must be in [auto_decline_pct, 1.0]. A tier that's
    BELOW the decline floor would silently invert accept/decline semantics.
    """
    with pytest.raises(ValueError, match=r"must be in \[auto_decline_pct"):
        _validate_best_offer_config(
            {
                "qty_tiers": {1: 0.95, "default": 0.50},  # 0.50 < decline 0.75
                "auto_decline_pct": 0.75,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_rejects_out_of_range_decline_pct() -> None:
    """auto_decline_pct = 1.5 is out of range → ValueError."""
    with pytest.raises(ValueError, match="auto_decline_pct"):
        _validate_best_offer_config(
            {
                "qty_tiers": {1: 0.95, "default": 0.90},
                "auto_decline_pct": 1.5,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_rejects_non_dict_qty_tiers() -> None:
    """qty_tiers MUST be a dict; reject lists / strs / scalars."""
    with pytest.raises(ValueError, match="must be dict"):
        _validate_best_offer_config(
            {
                "qty_tiers": [0.95, 0.925, 0.90],
                "auto_decline_pct": 0.75,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_rejects_zero_qty_key() -> None:
    """Numeric qty keys must be >= 1 (zero qty is meaningless on a Best Offer)."""
    with pytest.raises(ValueError, match="must be int"):
        _validate_best_offer_config(
            {
                "qty_tiers": {0: 0.95, "default": 0.90},
                "auto_decline_pct": 0.75,
                "round_down_to_pound": True,
            }
        )


def test_validate_best_offer_config_rejects_round_down_false() -> None:
    """Stage 5 follow-up: round_down_to_pound=False is rejected at validate
    time because the responder script (respond_best_offers.py) hardcodes
    math.floor() regardless of this flag — the two surfaces would silently
    diverge if false were allowed."""
    with pytest.raises(ValueError, match="round_down_to_pound=False is not currently supported"):
        _validate_best_offer_config(
            {
                "qty_tiers": {1: 0.95, 2: 0.925, "default": 0.90},
                "auto_decline_pct": 0.75,
                "round_down_to_pound": False,
            }
        )


def test_validate_best_offer_config_rejects_non_bool_round_down() -> None:
    """round_down_to_pound must be bool; reject str 'true', int 1, etc."""
    with pytest.raises(ValueError, match="round_down_to_pound"):
        _validate_best_offer_config(
            {
                "qty_tiers": {1: 0.95, "default": 0.90},
                "auto_decline_pct": 0.75,
                "round_down_to_pound": "true",
            }
        )


def test_validate_best_offer_config_accepts_absent_block() -> None:
    """Empty dict (absent block) is the fallback path — must NOT raise."""
    _validate_best_offer_config({})  # no exception


def test_validate_best_offer_config_accepts_well_formed_qty_tiers() -> None:
    """Canonical good config (Issue #30 default) — must NOT raise."""
    _validate_best_offer_config(
        {
            "qty_tiers": {1: 0.95, 2: 0.925, "default": 0.90},
            "auto_decline_pct": 0.75,
            "round_down_to_pound": True,
        }
    )


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


# ---------------------------------------------------------------------------
# Issue #30 AC2.4 — qty-tier dispatch + defensive entry validation
# ---------------------------------------------------------------------------


def test_qty_tier_dispatch_qty1_uses_95pct(isolated_fees_config) -> None:
    """qty=1 → qty_tiers[1] = 0.95 → floor(0.95 * 100) = 95."""
    isolated_fees_config(_QTY_TIER_CONFIG)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=100.0, quantity=1)
    assert r["auto_accept_gbp"] == 95  # 0.95 * 100 = 95
    assert "qty=1" in r["rationale"]


def test_qty_tier_dispatch_qty2_uses_925pct(isolated_fees_config) -> None:
    """qty=2 → qty_tiers[2] = 0.925 → floor(0.925 * 100) = 92."""
    isolated_fees_config(_QTY_TIER_CONFIG)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=100.0, quantity=2)
    assert r["auto_accept_gbp"] == 92  # 0.925 * 100 = 92.5 → floor → 92
    assert "qty=2" in r["rationale"]


def test_qty_tier_dispatch_qty3_uses_default_90pct(isolated_fees_config) -> None:
    """qty=3 → not in qty_tiers as int key → 'default' = 0.90 → floor(0.90 * 100) = 90."""
    isolated_fees_config(_QTY_TIER_CONFIG)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=100.0, quantity=3)
    assert r["auto_accept_gbp"] == 90
    assert "default" in r["rationale"]


def test_qty_tier_dispatch_qty10_falls_to_default(isolated_fees_config) -> None:
    """qty=10 (large bulk) → 'default' tier (0.90) — no special-case for qty>=4."""
    isolated_fees_config(_QTY_TIER_CONFIG)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=100.0, quantity=10)
    assert r["auto_accept_gbp"] == 90
    assert "default" in r["rationale"]


def test_compute_best_offer_thresholds_rejects_zero_quantity(isolated_fees_config) -> None:
    """quantity=0 violates 'qty must be a positive listing count'."""
    isolated_fees_config(_QTY_TIER_CONFIG)
    with pytest.raises(ValueError, match="quantity"):
        compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0, quantity=0)


def test_compute_best_offer_thresholds_rejects_negative_quantity(isolated_fees_config) -> None:
    isolated_fees_config(_QTY_TIER_CONFIG)
    with pytest.raises(ValueError, match="quantity"):
        compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0, quantity=-1)


def test_compute_best_offer_thresholds_default_quantity_is_qty1_tier(
    isolated_fees_config,
) -> None:
    """Caller omits `quantity` kwarg → defaults to 1 → qty_tiers[1] tier."""
    isolated_fees_config(_QTY_TIER_CONFIG)
    r = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0)
    # Same answer as explicit quantity=1
    explicit = compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=50.0, quantity=1)
    assert r["auto_accept_gbp"] == explicit["auto_accept_gbp"]


def test_compute_best_offer_thresholds_rejects_zero_or_negative_live_price(
    isolated_fees_config,
) -> None:
    """live_price_gbp <= 0 → ValueError. Catastrophic-accept bypass guard:
    without this, 0.95 * 0 = 0 floor = 0 silently auto-accepts every offer.
    """
    isolated_fees_config(_QTY_TIER_CONFIG)
    with pytest.raises(ValueError, match="live_price_gbp"):
        compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=0.0, quantity=1)
    with pytest.raises(ValueError, match="live_price_gbp"):
        compute_best_offer_thresholds(floor_gbp=8.0, live_price_gbp=-5.0, quantity=1)


def test_compute_best_offer_thresholds_rejects_sub_2_quid_listing(
    isolated_fees_config,
) -> None:
    """live_price_gbp < £2 → ValueError. floor(0.90 * 0.50) = £0 would push
    a £0 counter that eBay rejects with `price out of range`. Surface the
    operator's misconfigured listing instead of silently failing live.
    """
    isolated_fees_config(_QTY_TIER_CONFIG)
    with pytest.raises(ValueError, match="too low"):
        compute_best_offer_thresholds(floor_gbp=0.0, live_price_gbp=0.50, quantity=3)
    with pytest.raises(ValueError, match="too low"):
        compute_best_offer_thresholds(floor_gbp=0.0, live_price_gbp=1.99, quantity=1)
    # £2 is the minimum supported (boundary)
    r = compute_best_offer_thresholds(floor_gbp=0.0, live_price_gbp=2.0, quantity=1)
    assert r["auto_accept_gbp"] == 1  # floor(0.95 * 2) = 1
