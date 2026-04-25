"""
Fees + cost config loader.

Single read path for `config/fees.yaml`. Cached at first call.
Floor-price math (ebay/analytics.py) and the update_listing guardrail
(server.py Phase 4) both read through _load_fees_config().
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "fees.yaml"


@lru_cache(maxsize=1)
def _load_fees_config() -> dict[str, Any]:
    """Load and cache fees.yaml. Override path via EBAY_FEES_CONFIG env var (tests)."""
    path = Path(os.environ.get("EBAY_FEES_CONFIG", _DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(f"fees config missing: {path} — expected at config/fees.yaml")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _validate(data, path)
    return data


def reset_fees_cache() -> None:
    """Clear the cached config — tests that swap EBAY_FEES_CONFIG call this."""
    _load_fees_config.cache_clear()


_REQUIRED_KEYS = {
    "ebay_uk": ("fvf_rate", "per_order_fee_gbp", "marketplace_id", "site_id"),
    "postage": ("outbound_gbp", "return_gbp"),
    "time_cost": ("mode", "sale_gbp", "return_gbp", "hourly_rate_gbp"),
    "defaults": ("cogs_gbp", "return_rate", "target_margin"),
    "under_pricing": (
        "velocity_median_default",
        "recommended_band_low_pct",
        "recommended_band_high_pct",
    ),
}


def _validate(data: dict, path: Path) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    if "packaging_gbp" not in data:
        raise ValueError(f"{path}: missing packaging_gbp")
    for section, keys in _REQUIRED_KEYS.items():
        if section not in data or not isinstance(data[section], dict):
            raise ValueError(f"{path}: missing section {section!r}")
        missing = [k for k in keys if k not in data[section]]
        if missing:
            raise ValueError(f"{path}: section {section!r} missing keys {missing}")
    mode = data["time_cost"]["mode"]
    if mode not in ("sunk", "marginal"):
        raise ValueError(f"{path}: time_cost.mode={mode!r} — must be 'sunk' or 'marginal'")
