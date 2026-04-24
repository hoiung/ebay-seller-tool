"""Unit tests for ebay.hdd_specs (P1.9 seed validation)."""

import pytest

from ebay.hdd_specs import HDD_SPECS

REQUIRED_SUBKEYS = {
    "brand",
    "family",
    "capacity",
    "rpm",
    "interface",
    "transfer_rate",
    "cache",
    "form_factor",
    "height",
}


def test_hdd_specs_non_empty() -> None:
    assert len(HDD_SPECS) >= 1


@pytest.mark.parametrize("key", list(HDD_SPECS.keys()))
def test_hdd_specs_row_shape(key: str) -> None:
    row = HDD_SPECS[key]
    assert isinstance(row, dict)
    assert set(row.keys()) == REQUIRED_SUBKEYS, (
        f"{key}: subkeys {set(row.keys()) ^ REQUIRED_SUBKEYS} diverge from contract"
    )


@pytest.mark.parametrize("key", list(HDD_SPECS.keys()))
def test_hdd_specs_row_values_non_null_except_3_5_height(key: str) -> None:
    row = HDD_SPECS[key]
    for k, v in row.items():
        if k == "height" and row["form_factor"] == "3.5 in":
            # None allowed — 3.5" drives have no short/tall variant
            continue
        assert v is not None, f"{key}.{k} is None — only 3.5 in height may be None"


@pytest.mark.parametrize("key", list(HDD_SPECS.keys()))
def test_hdd_specs_form_factor_allowed(key: str) -> None:
    assert HDD_SPECS[key]["form_factor"] in {"2.5 in", "3.5 in"}


@pytest.mark.parametrize("key", list(HDD_SPECS.keys()))
def test_hdd_specs_height_allowed(key: str) -> None:
    height = HDD_SPECS[key]["height"]
    # 7mm is the BarraCuda 2.5" slim z-height (e.g. ST2000LM015 per 100807728a.pdf).
    assert height in {"15mm", "9.5mm", "7mm", None}
    if height is None:
        assert HDD_SPECS[key]["form_factor"] == "3.5 in"
