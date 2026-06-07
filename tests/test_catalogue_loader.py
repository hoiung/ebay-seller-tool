"""Phase 0 — generic runtime loader fail-loud + shape + cache contract.

The loader ships NO product data in the public repo; it reads the private
catalogue + listing-contract + taxonomy from EBAY_LISTING_DATA_DIR. conftest
points that env at ebay/listing_data.example (synthetic). These tests assert
the 3-layer fail-loud (unset / missing / empty-or-malformed), the row-shape
validation, and the cache+reset seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ebay import catalogue_loader as cl

_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "ebay" / "listing_data.example"


@pytest.fixture(autouse=True)
def _isolate_loader_caches():
    """Each test starts and ends with cleared loader caches (lru_cache persists
    across tests otherwise, leaking a previous env's data)."""
    cl.reset_caches()
    yield
    cl.reset_caches()


def test_load_listing_data_returns_catalogue_and_contract():
    data = cl.load_listing_data()
    assert set(data) == {"catalogue", "contract"}
    assert isinstance(data["catalogue"], dict) and data["catalogue"]
    # every row carries the required generic sub-key shape
    for key, row in data["catalogue"].items():
        for required in cl._REQUIRED_CATALOGUE_KEYS:
            assert required in row, f"{key} missing {required}"
    contract = data["contract"]
    assert contract["schema"] == cl.CONTRACT_SCHEMA
    assert isinstance(contract["item_specifics"], list) and contract["item_specifics"]
    assert "default" in contract["transfer_rate"]
    assert {"with_caddy", "without_caddy"} <= contract["storage_format"].keys()


def test_loader_fails_loud_when_path_unset(monkeypatch):
    monkeypatch.delenv("EBAY_LISTING_DATA_DIR", raising=False)
    cl.reset_caches()
    with pytest.raises(cl.ListingDataError, match="EBAY_LISTING_DATA_DIR is not set"):
        cl.load_listing_data()


def test_loader_fails_loud_when_path_missing(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("EBAY_LISTING_DATA_DIR", str(missing))
    cl.reset_caches()
    with pytest.raises(cl.ListingDataError) as exc:
        cl.load_listing_data()
    # the error echoes the resolved path so the operator can fix it
    assert str(missing) in str(exc.value)


def test_loader_fails_loud_when_empty_or_malformed(monkeypatch, tmp_path):
    monkeypatch.setenv("EBAY_LISTING_DATA_DIR", str(tmp_path))
    # contract present so the catalogue is the one under test
    (tmp_path / cl.CONTRACT_FILENAME).write_text(
        (_EXAMPLE_DIR / cl.CONTRACT_FILENAME).read_text(), encoding="utf-8"
    )

    # (a) empty catalogue mapping -> raise (NEVER silently returns {})
    (tmp_path / cl.CATALOGUE_FILENAME).write_text(
        "schema: listing-catalogue-v1\ncatalogue: {}\n", encoding="utf-8"
    )
    cl.reset_caches()
    with pytest.raises(cl.ListingDataError, match="non-empty mapping"):
        cl.load_listing_data()

    # (b) wrong schema sentinel -> raise
    (tmp_path / cl.CATALOGUE_FILENAME).write_text(
        "schema: WRONG-SCHEMA\ncatalogue: {}\n", encoding="utf-8"
    )
    cl.reset_caches()
    with pytest.raises(cl.ListingDataError, match="schema:"):
        cl.load_listing_data()

    # (c) malformed YAML -> raise
    (tmp_path / cl.CATALOGUE_FILENAME).write_text("schema: [unterminated\n", encoding="utf-8")
    cl.reset_caches()
    with pytest.raises(cl.ListingDataError, match="malformed YAML"):
        cl.load_listing_data()


def test_loader_validates_row_shape(monkeypatch, tmp_path):
    """A catalogue row missing a required sub-key fails loud (AC 0.4)."""
    monkeypatch.setenv("EBAY_LISTING_DATA_DIR", str(tmp_path))
    (tmp_path / cl.CONTRACT_FILENAME).write_text(
        (_EXAMPLE_DIR / cl.CONTRACT_FILENAME).read_text(), encoding="utf-8"
    )
    # row is missing 'cache'
    (tmp_path / cl.CATALOGUE_FILENAME).write_text(
        "schema: listing-catalogue-v1\n"
        "catalogue:\n"
        "  ROW-1:\n"
        "    brand: X\n"
        "    family: Y\n"
        "    capacity: 1TB\n"
        "    rpm: 7200 RPM\n"
        "    interface: Bus\n"
        "    transfer_rate: RATE-MID\n"
        "    form_factor: 3.5 in\n"
        "    height: null\n",
        encoding="utf-8",
    )
    cl.reset_caches()
    with pytest.raises(cl.ListingDataError, match="missing required sub-key 'cache'"):
        cl.load_listing_data()


def test_loader_caches_and_reset_isolates(monkeypatch, tmp_path):
    """Repeated loads return the cached object; reset + env swap yields new data
    (AC 0.5 — the shared reset seam isolates tests)."""
    # first load from the example dir (conftest env)
    first = cl.load_listing_data()["catalogue"]
    again = cl.load_listing_data()["catalogue"]
    assert first is again  # cached — same object identity

    # build a one-row alternate dir and swap to it WITHOUT reset -> still cached
    (tmp_path / cl.CONTRACT_FILENAME).write_text(
        (_EXAMPLE_DIR / cl.CONTRACT_FILENAME).read_text(), encoding="utf-8"
    )
    (tmp_path / cl.CATALOGUE_FILENAME).write_text(
        "schema: listing-catalogue-v1\n"
        "catalogue:\n"
        "  ONLY-ROW:\n"
        "    brand: Z\n"
        "    family: Zeta\n"
        "    capacity: 9TB\n"
        "    rpm: 7200 RPM\n"
        "    interface: Bus\n"
        "    transfer_rate: RATE-MID\n"
        "    cache: 1 MB\n"
        "    form_factor: 3.5 in\n"
        "    height: null\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EBAY_LISTING_DATA_DIR", str(tmp_path))
    assert cl.load_listing_data()["catalogue"] is first  # cache not yet cleared

    cl.reset_caches()
    swapped = cl.load_listing_data()["catalogue"]
    assert swapped is not first
    assert set(swapped) == {"ONLY-ROW"}
