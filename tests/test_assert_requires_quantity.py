"""Unit tests for ebay.listings._assert_requires_quantity (P1.5)."""

import pytest

from ebay.listings import _assert_requires_quantity


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"Item": {}},
        {"Item": {"Quantity": None}},
        {"Item": {"Quantity": 0}},
        {"Item": {"Quantity": "0"}},
        {"Item": {"Quantity": -1}},
        {"Item": {"Quantity": "-5"}},
        {"Item": {"Quantity": "not-a-number"}},
        {"Item": {"Quantity": [1, 2]}},
    ],
)
def test_assert_requires_quantity_rejects_bad(payload: dict) -> None:
    with pytest.raises(ValueError, match="SAFETY: Add"):
        _assert_requires_quantity(payload)


@pytest.mark.parametrize("quantity", [1, 100, "1", "1000"])
def test_assert_requires_quantity_accepts_good(quantity: object) -> None:
    payload = {"Item": {"Quantity": quantity}}
    _assert_requires_quantity(payload)  # no raise


def test_assert_requires_quantity_custom_min() -> None:
    payload = {"Item": {"Quantity": 5}}
    with pytest.raises(ValueError, match=r"Add Quantity=5 < min=10"):
        _assert_requires_quantity(payload, min=10)
