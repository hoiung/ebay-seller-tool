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


# ---------------------------------------------------------------------------
# #40 AC5.1 — Seagate HARD CONTRACT value-guard.
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
    "ST2000NX0303": "Enterprise Capacity",
    "ST2000NX0273": "Enterprise Capacity",
    "ST2000NX0253": "Enterprise Capacity",
    "ST2000NX0253-EXOS": "Exos 7E2000",
    "ST2000NX0403": "Exos 7E2000",
    "ST4000NM0035": "Enterprise Capacity",
    "ST4000LM016": "BarraCuda",
    "ST2000LM015": "BarraCuda",
    "ST3000NM0033": "Constellation ES.3",
    "EG1200JEMDA": "Enterprise Performance 10K.8",
    "HUS724020ALA640": "Ultrastar 7K4000",
    "HUS724030ALA640": "Ultrastar 7K4000",
    "HUS726040ALA614": "Ultrastar 7K6000",
    "MG04ACA400N": "MG04 Series",
    "AL14SEB090N": "AL14SE",
    "HUC101030CSS600": "Ultrastar C10K600",
}


def test_expected_family_map_covers_every_row() -> None:
    """A new HDD_SPECS row MUST add a pinned physical-label series here — this
    coverage guard forces a human to confirm the contract for new drives rather
    than letting an un-pinned family slip through."""
    assert set(_EXPECTED_FAMILY) == set(HDD_SPECS), (
        f"golden family map out of sync with HDD_SPECS: "
        f"{set(_EXPECTED_FAMILY) ^ set(HDD_SPECS)}"
    )


@pytest.mark.parametrize("key", list(HDD_SPECS.keys()))
def test_hdd_specs_family_equals_physical_label_series(key: str) -> None:
    """Each row's family must equal the series printed on the physical drive
    label (Seagate HARD CONTRACT) — not an OEM/HPE option/spare designation."""
    assert HDD_SPECS[key]["family"] == _EXPECTED_FAMILY[key], (
        f"{key}: family {HDD_SPECS[key]['family']!r} != physical-label series "
        f"{_EXPECTED_FAMILY[key]!r} — relabel breach of the HARD CONTRACT"
    )


def test_family_guard_catches_oem_hpe_substitution() -> None:
    """Non-vacuity proof: substituting an OEM/HPE-derived family for the
    physical-label series is detected by the same equality the guard uses."""
    key = "ST2000NX0253-EXOS"
    assert HDD_SPECS[key]["family"] == _EXPECTED_FAMILY[key]  # real series passes
    # An HPE option/spare-style designation is NOT the physical series.
    hpe_derived_row = {**HDD_SPECS[key], "family": "MB2000GFEMH"}
    assert hpe_derived_row["family"] != _EXPECTED_FAMILY[key], (
        "guard must reject an OEM/HPE-derived family substitution"
    )
