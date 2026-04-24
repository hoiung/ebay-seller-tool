"""Unit tests for ebay.store (Issue #13 Phase 1.5)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ebay import store


def _run(coro):
    return asyncio.run(coro)


def _reply(**kwargs: object) -> SimpleNamespace:
    """Build a fake ebaysdk Response wrapper matching execute_with_retry contract."""
    return SimpleNamespace(reply=SimpleNamespace(**kwargs))


def test_get_store_info_happy_path() -> None:
    """Store with 3 custom categories → categories_count==3, all fields populated."""
    fake_store = SimpleNamespace(
        Name="My Test Store",
        CustomCategories=SimpleNamespace(
            CustomCategory=[
                SimpleNamespace(CategoryID="111", Name="Hard Drives", Order="1"),
                SimpleNamespace(CategoryID="222", Name="SSD", Order="2"),
                SimpleNamespace(CategoryID="333", Name="NIC", Order="3"),
            ]
        ),
    )
    with patch("ebay.store.execute_with_retry", return_value=_reply(Store=fake_store)):
        result = _run(store.fetch_store_info())

    assert result["store_name"] == "My Test Store"
    assert result["categories_count"] == 3
    assert len(result["store_categories"]) == 3
    assert result["store_categories"][0] == {
        "category_id": "111",
        "category_name": "Hard Drives",
        "category_order": 1,
    }
    assert result["store_categories"][2]["category_order"] == 3


def test_get_store_info_no_categories() -> None:
    """Phase 1.5.3 — store with 0 custom categories surfaces categories_count=0.

    This is the compound-signal trigger documented in Doc 14 L165 — zero
    custom categories indicates cross-promotion disabled at root.
    """
    fake_store = SimpleNamespace(Name="Empty Store", CustomCategories=None)
    with patch("ebay.store.execute_with_retry", return_value=_reply(Store=fake_store)):
        result = _run(store.fetch_store_info())

    assert result["store_name"] == "Empty Store"
    assert result["categories_count"] == 0
    assert result["store_categories"] == []


def test_get_store_info_single_category_normalisation() -> None:
    """ebaysdk returns a single-element list as a bare object, not a list — normalise."""
    fake_store = SimpleNamespace(
        Name="Solo Cat Store",
        CustomCategories=SimpleNamespace(
            CustomCategory=SimpleNamespace(CategoryID="999", Name="Only", Order="1"),
        ),
    )
    with patch("ebay.store.execute_with_retry", return_value=_reply(Store=fake_store)):
        result = _run(store.fetch_store_info())

    assert result["categories_count"] == 1
    assert result["store_categories"][0]["category_id"] == "999"


def test_get_store_info_no_store_object() -> None:
    """Defensive: if API returns no Store node, return empty defaults — no raise."""
    with patch("ebay.store.execute_with_retry", return_value=_reply()):
        result = _run(store.fetch_store_info())

    assert result["store_name"] is None
    assert result["store_categories"] == []
    assert result["categories_count"] == 0


def test_get_store_info_malformed_order_falls_back_none() -> None:
    """Non-integer Order value → category_order=None, no raise."""
    fake_store = SimpleNamespace(
        Name="Bad Order Store",
        CustomCategories=SimpleNamespace(
            CustomCategory=SimpleNamespace(CategoryID="1", Name="Cat", Order="not-a-number"),
        ),
    )
    with patch("ebay.store.execute_with_retry", return_value=_reply(Store=fake_store)):
        result = _run(store.fetch_store_info())

    assert result["store_categories"][0]["category_order"] is None


def test_get_store_info_api_error_propagates() -> None:
    """Phase 1.5.3 — execute_with_retry raises (auth failure, network) → caller sees it."""
    with patch("ebay.store.execute_with_retry", side_effect=RuntimeError("auth failed")):
        with pytest.raises(RuntimeError, match="auth failed"):
            _run(store.fetch_store_info())
