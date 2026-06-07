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
    # 7mm is the Series-Gamma 2.5" slim z-height (e.g. MDL-A07 per 100807728a.pdf).
    assert height in {"15mm", "9.5mm", "7mm", None}
    if height is None:
        assert HDD_SPECS[key]["form_factor"] == "3.5 in"


# ---------------------------------------------------------------------------
# #40 AC5.1 — Fabrikam HARD CONTRACT value-guard.
# The series name printed on the PHYSICAL drive label is the source of truth
# for `family` — never an OEM/HPE option or spare designation. The pre-existing
# shape tests above only check that `family` EXISTS, so the exact relabel
# failure mode the contract forbids (swapping the physical series for an
# HPE-derived family) would pass them silently. This golden map pins each row's
# family to the physical-label series so a substitution fails loudly. Each value
# is hand-confirmed against the physical drive label (the contract anchor) — it
# is an INDEPENDENT assertion of the contract, not a re-derivation of production.
# ---------------------------------------------------------------------------

_EXPECTED_FAMILY = {
    "MDL-A01": "Series-Alpha",
    "MDL-A02": "Series-Alpha",
    "MDL-A03": "Series-Alpha",
    "MDL-A03-VAR": "Series-Beta-7X2000",
    "MDL-A04": "Series-Beta-7X2000",
    "MDL-A05": "Series-Alpha",
    "MDL-A06": "Series-Gamma",
    "MDL-A07": "Series-Gamma",
    "MDL-A08": "Series-Delta-3",
    "MDL-A09": "Series-Delta",
    "MDL-A10": "Series-Epsilon",
    "MDL-A11": "Series-Zeta-4000",
    "MDL-A12": "Series-Zeta-4000",
    "MDL-A13": "Series-Zeta-6000",
    "MDL-A14": "Series-MGA",
    "MDL-A15": "MDL-A15",
    "MDL-A16": "Series-Zeta-C600",
    "MDL-A17": "Series-Delta-3",
    "MDL-A18": "Series-MGB",
    "MDL-A19": "RE4",
    "MDL-A20": "Series-Delta",
    "MDL-A21": "Series-Delta-2",
}


def test_expected_family_map_covers_every_row() -> None:
    """A new HDD_SPECS row MUST add a pinned physical-label series here — this
    coverage guard forces a human to confirm the contract for new drives rather
    than letting an un-pinned family slip through."""
    assert set(_EXPECTED_FAMILY) == set(HDD_SPECS), (
        f"golden family map out of sync with HDD_SPECS: {set(_EXPECTED_FAMILY) ^ set(HDD_SPECS)}"
    )


@pytest.mark.parametrize("key", list(HDD_SPECS.keys()))
def test_hdd_specs_family_equals_physical_label_series(key: str) -> None:
    """Each row's family must equal the series printed on the physical drive
    label (Fabrikam HARD CONTRACT) — not an OEM/HPE option/spare designation."""
    assert HDD_SPECS[key]["family"] == _EXPECTED_FAMILY[key], (
        f"{key}: family {HDD_SPECS[key]['family']!r} != physical-label series "
        f"{_EXPECTED_FAMILY[key]!r} — relabel breach of the HARD CONTRACT"
    )


def test_family_guard_catches_oem_hpe_substitution() -> None:
    """Non-vacuity proof: substituting an OEM/HPE-derived family for the
    physical-label series is detected by the same equality the guard uses."""
    key = "MDL-A03-VAR"
    assert HDD_SPECS[key]["family"] == _EXPECTED_FAMILY[key]  # real series passes
    # An HPE option/spare-style designation is NOT the physical series.
    hpe_derived_row = {**HDD_SPECS[key], "family": "MDL-A23"}
    assert hpe_derived_row["family"] != _EXPECTED_FAMILY[key], (
        "guard must reject an OEM/HPE-derived family substitution"
    )
