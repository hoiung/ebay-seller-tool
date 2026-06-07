"""Tests for ebay.listings.compute_diff — the update_listing guardrail diff.

Covers #40 AC2.6 (condition_description / item_specifics must only be reported
as changed when the `before` snapshot carries the field AND it differs — they
were previously emitted unconditionally whenever the kwarg was non-None, so a
revise re-sending the SAME value over-reported "changed") and AC5.2 (a real
test seam for compute_diff, which previously had none).
"""

from __future__ import annotations

import hashlib

from ebay.listings import compute_diff

_BEFORE = {
    "title": "Fabrikam Series-Beta 2TB",
    "price": "35.00",
    "condition_id": "3000",
}


def _desc_before(html: str) -> dict:
    b = dict(_BEFORE)
    b["description_hash"] = hashlib.sha256(html.encode()).hexdigest()[:16]
    b["description_length"] = len(html)
    return b


def test_no_change_yields_empty_diff() -> None:
    diff = compute_diff(dict(_BEFORE), title="Fabrikam Series-Beta 2TB", description_html=None, price=35.0)
    assert diff == {}


def test_title_change_detected() -> None:
    diff = compute_diff(dict(_BEFORE), title="New Title", description_html=None, price=None)
    assert diff["title"] == {"before": "Fabrikam Series-Beta 2TB", "after": "New Title"}


def test_price_change_detected() -> None:
    diff = compute_diff(dict(_BEFORE), title=None, description_html=None, price=30.0)
    assert diff["price"]["after"] == "30.0"


def test_price_unchanged_not_reported_float_equality() -> None:
    # eBay stores "35.00", Python str(35.0)=="35.0" — must compare as floats.
    diff = compute_diff(dict(_BEFORE), title=None, description_html=None, price=35.0)
    assert "price" not in diff


# --- #40 AC2.6: condition_description ---------------------------------------


def test_condition_description_unchanged_not_in_diff() -> None:
    before = dict(_BEFORE)
    before["condition_description"] = "Pull from working server"
    diff = compute_diff(
        before,
        title=None,
        description_html=None,
        price=None,
        condition_description="Pull from working server",
    )
    assert "condition_description" not in diff


def test_condition_description_changed_in_diff() -> None:
    before = dict(_BEFORE)
    before["condition_description"] = "Old notes"
    diff = compute_diff(
        before,
        title=None,
        description_html=None,
        price=None,
        condition_description="New notes",
    )
    assert diff["condition_description"] == {"before": "Old notes", "after": "New notes"}


def test_condition_description_omitted_when_before_lacks_field() -> None:
    # `before` has no condition_description → no true comparison → OMIT (do not
    # claim a change). This is the over-report fix: pre-fix this added the field.
    diff = compute_diff(
        dict(_BEFORE),
        title=None,
        description_html=None,
        price=None,
        condition_description="Seller notes",
    )
    assert "condition_description" not in diff


# --- #40 AC2.6: item_specifics ---------------------------------------------


def test_item_specifics_unchanged_not_in_diff() -> None:
    before = dict(_BEFORE)
    before["item_specifics"] = {"Brand": "Fabrikam", "Capacity": "2TB"}
    diff = compute_diff(
        before,
        title=None,
        description_html=None,
        price=None,
        item_specifics={"Brand": "Fabrikam", "Capacity": "2TB"},
    )
    assert "item_specifics" not in diff


def test_item_specifics_identical_resend_list_snapshot_not_in_diff() -> None:
    """#44 follow-up — the REAL snapshot path list-wraps values; a scalar re-send
    of the SAME values must report no change (no spurious idempotent revise).
    compute_diff normalises both sides + compares the merged result vs current."""
    before = dict(_BEFORE)
    before["item_specifics"] = {"Country of Origin": ["Thailand"], "Brand": ["Fabrikam"]}
    # caller passes a scalar partial that matches live exactly
    diff = compute_diff(
        before,
        title=None,
        description_html=None,
        price=None,
        item_specifics={"Country of Origin": "Thailand"},
    )
    assert "item_specifics" not in diff


def test_item_specifics_real_change_against_list_snapshot_in_diff() -> None:
    """Counterpart — a genuine value change against the list-valued snapshot IS
    detected (proves the normalisation didn't mask real changes)."""
    before = dict(_BEFORE)
    before["item_specifics"] = {"Country of Origin": ["China"], "Brand": ["Fabrikam"]}
    diff = compute_diff(
        before,
        title=None,
        description_html=None,
        price=None,
        item_specifics={"Country of Origin": "Thailand"},
    )
    assert "item_specifics" in diff


def test_item_specifics_changed_in_diff() -> None:
    before = dict(_BEFORE)
    before["item_specifics"] = {"Brand": "Fabrikam"}
    diff = compute_diff(
        before,
        title=None,
        description_html=None,
        price=None,
        item_specifics={"Brand": "Fabrikam", "Capacity": "2TB"},
    )
    assert diff["item_specifics"] == {"before_count": 1, "after_count": 2}


def test_item_specifics_omitted_when_before_lacks_field() -> None:
    diff = compute_diff(
        dict(_BEFORE),
        title=None,
        description_html=None,
        price=None,
        item_specifics={"Brand": "Fabrikam"},
    )
    assert "item_specifics" not in diff


def test_description_change_detected_by_hash() -> None:
    before = _desc_before("<p>old</p>")
    diff = compute_diff(before, title=None, description_html="<p>new</p>", price=None)
    assert "description_html" in diff
    assert diff["description_html"]["after_length"] == len("<p>new</p>")
