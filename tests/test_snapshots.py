"""Unit tests for ebay.snapshots (Issue #13 Phase 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebay import snapshots


def _set_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect snapshot path to tmp_path/snap.jsonl via env override."""
    target = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(target))
    return target


def test_append_snapshot_creates_file_and_writes_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = _set_path(monkeypatch, tmp_path)
    assert not target.exists()

    snapshots.append_snapshot(
        "analysis_baseline",
        "12345",
        {"price_gbp": 35.0, "watch_count": 2},
    )

    assert target.exists()
    lines = target.read_text().strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["item_id"] == "12345"
    assert row["event"] == "analysis_baseline"
    assert row["price_gbp"] == 35.0
    assert row["watch_count"] == 2
    assert "timestamp" in row


def test_append_snapshot_appends_multiple(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "12345", {"price_gbp": 30.0})
    snapshots.append_snapshot("price_change", "12345", {"price_gbp": 35.0})
    snapshots.append_snapshot("post_change_check", "12345", {"price_gbp": 35.0})

    lines = target.read_text().strip().split("\n")
    assert len(lines) == 3
    events = [json.loads(line)["event"] for line in lines]
    assert events == ["analysis_baseline", "price_change", "post_change_check"]


def test_append_snapshot_creates_parent_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 5.1.1 — parent directory is auto-created."""
    nested = tmp_path / "deeply" / "nested" / "path" / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(nested))

    snapshots.append_snapshot("analysis_baseline", "999", {})

    assert nested.exists()
    assert nested.parent.exists()


def test_append_snapshot_rejects_invalid_event_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="event_type must be one of"):
        snapshots.append_snapshot("BOGUS_EVENT", "999", {})


def test_append_snapshot_rejects_empty_item_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="item_id required"):
        snapshots.append_snapshot("analysis_baseline", "", {})


def test_compute_elasticity_returns_none_when_events_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "999", {"price_gbp": 30.0})
    # post_change_check missing
    result = snapshots.compute_elasticity("999", "analysis_baseline", "post_change_check")
    assert result is None


def test_compute_elasticity_returns_none_when_file_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_path(monkeypatch, tmp_path)
    result = snapshots.compute_elasticity("999", "analysis_baseline", "post_change_check")
    assert result is None


def test_compute_elasticity_price_sensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """+10% price → -5 watchers → elasticity = -5/10 = -0.5 → inconclusive.

    Build a strong-elasticity case: +10% price, -25 watchers → -2.5 → price_sensitive.
    """
    _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "999", {"price_gbp": 30.0, "watch_count": 30})
    snapshots.append_snapshot("post_change_check", "999", {"price_gbp": 33.0, "watch_count": 5})

    result = snapshots.compute_elasticity("999", "analysis_baseline", "post_change_check")
    assert result is not None
    assert result["delta_price_pct"] == 10.0  # (33-30)/30 * 100
    assert result["delta_watchers"] == -25
    assert result["elasticity"] == -2.5
    assert result["classification"] == "price_sensitive"


def test_compute_elasticity_inelastic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """+10% price → -1 watcher → elasticity = -0.1 → inelastic."""
    _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "999", {"price_gbp": 30.0, "watch_count": 30})
    snapshots.append_snapshot("post_change_check", "999", {"price_gbp": 33.0, "watch_count": 29})

    result = snapshots.compute_elasticity("999", "analysis_baseline", "post_change_check")
    assert result is not None
    assert result["elasticity"] == -0.1
    assert result["classification"] == "inelastic"


def test_compute_elasticity_filters_by_item_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Multi-item file: only events for the requested item_id are considered."""
    _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "111", {"price_gbp": 30.0, "watch_count": 5})
    snapshots.append_snapshot("analysis_baseline", "222", {"price_gbp": 50.0, "watch_count": 10})
    snapshots.append_snapshot("post_change_check", "111", {"price_gbp": 33.0, "watch_count": 3})

    # 222 has no post_change_check → None
    assert snapshots.compute_elasticity("222", "analysis_baseline", "post_change_check") is None
    # 111 has both events
    result = snapshots.compute_elasticity("111", "analysis_baseline", "post_change_check")
    assert result is not None
    assert result["item_id"] == "111"


def test_compute_elasticity_zero_before_price_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defensive: before_price=0 would cause div-by-zero — return None."""
    _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "999", {"price_gbp": 0.0, "watch_count": 0})
    snapshots.append_snapshot("post_change_check", "999", {"price_gbp": 30.0, "watch_count": 5})
    result = snapshots.compute_elasticity("999", "analysis_baseline", "post_change_check")
    assert result is None


def test_compute_elasticity_zero_price_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same-price events → delta_price_pct=0 → elasticity=0 → inelastic."""
    _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "999", {"price_gbp": 30.0, "watch_count": 5})
    snapshots.append_snapshot("post_change_check", "999", {"price_gbp": 30.0, "watch_count": 7})
    result = snapshots.compute_elasticity("999", "analysis_baseline", "post_change_check")
    assert result is not None
    assert result["delta_price_pct"] == 0.0
    assert result["elasticity"] == 0.0
    assert result["classification"] == "inelastic"


# ---- #21 Phase 2: weekly_snapshot event type + schema contract ----

# Canonical Phase 2 field enumeration — single source of truth for the schema
# contract. v1-consumed = read by Phase 4 classifier or Phase 6 markdown report.
# v2-deferred = write-only in v1, reserved for the v2 cluster classifier so it
# doesn't need a JSONL backfill.
WEEKLY_SNAPSHOT_V1_CONSUMED_FIELDS = {
    "week_id", "previous_price", "delta_pct", "floor_price_gbp",
    "imp_30d", "views_30d", "ctr_pct", "conv_pct", "tx_30d",
    "days_on_site", "decision", "decision_rationale", "consecutive_drops",
    "previous_decision", "manual_hold_flag", "data_quality_caveat",
    "applied_at",
}

WEEKLY_SNAPSHOT_V2_DEFERRED_FIELDS = {
    "mpn", "drive_type", "product_line", "condition_id", "cond_name",
    "watchers", "sold_lifetime", "rank_health_status", "audit_doc_cite",
    "elasticity_classification_at_snapshot", "authorized_by",
}


def _sample_weekly_snapshot_payload() -> dict:
    """Reference v1-consumed + v2-deferred field set for the Phase 2 schema."""
    return {
        # Base-row fields (event + item_id + timestamp injected by append_snapshot)
        "price_gbp": 49.99,
        "quantity": None,
        "watch_count": None,
        "view_count": None,
        "traffic_30d": None,
        "source": "weekly_orchestrator_v1",
        # v1-consumed
        "week_id": "2026-W18",
        "previous_price": 54.99,
        "delta_pct": -9.09,
        "floor_price_gbp": 35.00,
        "imp_30d": 1250,
        "views_30d": 18,
        "ctr_pct": 1.44,
        "conv_pct": 0.0,
        "tx_30d": 0,
        "days_on_site": 30,
        "decision": "DROP_5",
        "decision_rationale": "F4 action gate met (CTR 1.44%, views 18); F5 5%-band default",
        "consecutive_drops": 1,
        "previous_decision": "HOLD",
        "manual_hold_flag": False,
        "data_quality_caveat": "post-#29-toggle-window",
        "applied_at": None,
        # v2-deferred
        "mpn": "ST2000NX0253",
        "drive_type": "HDD",
        "product_line": "Enterprise Capacity",
        "condition_id": 3000,
        "cond_name": "Used",
        "watchers": 0,
        "sold_lifetime": 5,
        "rank_health_status": "stalled",
        "audit_doc_cite": "14_SALES_IMPROVEMENT_2026-04-24.md#row-7",
        "elasticity_classification_at_snapshot": None,
        "authorized_by": None,
    }


def test_valid_event_types_accepts_weekly_snapshot() -> None:
    """#21 Phase 2 — weekly_snapshot is in the canonical event-type enum."""
    assert "weekly_snapshot" in snapshots._VALID_EVENT_TYPES
    # Pre-existing 3 retained:
    assert {"analysis_baseline", "price_change", "post_change_check"}.issubset(
        snapshots._VALID_EVENT_TYPES
    )


def test_append_snapshot_accepts_weekly_snapshot_event_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """append_snapshot's _VALID_EVENT_TYPES gate now passes weekly_snapshot."""
    _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot(
        "weekly_snapshot",
        "287192992984",
        _sample_weekly_snapshot_payload(),
    )  # must not raise


def test_append_snapshot_rejects_unknown_event_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unknown event types still rejected after Phase 2 enum extension."""
    _set_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="event_type must be one of"):
        snapshots.append_snapshot("typo_snapshot", "111", {"price_gbp": 1.0})


def test_weekly_snapshot_schema_v1_fields_present_after_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#21 Phase 2 — every v1-consumed field MUST round-trip through JSONL."""
    target = _set_path(monkeypatch, tmp_path)
    payload = _sample_weekly_snapshot_payload()
    snapshots.append_snapshot("weekly_snapshot", "287192992984", payload)

    row = json.loads(target.read_text().strip())
    for field in WEEKLY_SNAPSHOT_V1_CONSUMED_FIELDS:
        assert field in row, f"v1-consumed field {field!r} missing from row"


def test_weekly_snapshot_schema_v2_deferred_fields_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#21 Phase 2 — v2-deferred fields are write-only in v1 but MUST round-trip
    when populated, so v2 cluster classifier doesn't need a JSONL backfill."""
    target = _set_path(monkeypatch, tmp_path)
    payload = _sample_weekly_snapshot_payload()
    snapshots.append_snapshot("weekly_snapshot", "287192992984", payload)

    row = json.loads(target.read_text().strip())
    for field in WEEKLY_SNAPSHOT_V2_DEFERRED_FIELDS:
        assert field in row, f"v2-deferred field {field!r} missing from row"


def test_weekly_snapshot_v1_field_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#21 Phase 2 — type test on each v1-consumed field."""
    target = _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot(
        "weekly_snapshot", "287192992984", _sample_weekly_snapshot_payload()
    )
    row = json.loads(target.read_text().strip())

    # String fields
    assert isinstance(row["week_id"], str)
    assert isinstance(row["decision"], str)
    assert isinstance(row["decision_rationale"], str)
    assert row["previous_decision"] is None or isinstance(row["previous_decision"], str)
    assert row["data_quality_caveat"] is None or isinstance(row["data_quality_caveat"], str)
    assert row["applied_at"] is None or isinstance(row["applied_at"], str)

    # Numeric fields (allow int/float; nullable for previous_price/delta_pct)
    assert row["previous_price"] is None or isinstance(row["previous_price"], (int, float))
    assert row["delta_pct"] is None or isinstance(row["delta_pct"], (int, float))
    assert isinstance(row["floor_price_gbp"], (int, float))
    assert isinstance(row["imp_30d"], int)
    assert isinstance(row["views_30d"], int)
    assert isinstance(row["ctr_pct"], (int, float))
    assert isinstance(row["conv_pct"], (int, float))
    assert isinstance(row["tx_30d"], int)
    assert isinstance(row["days_on_site"], int)
    assert isinstance(row["consecutive_drops"], int)

    # Bool field — must check bool BEFORE int since bool is a subclass of int.
    assert isinstance(row["manual_hold_flag"], bool)


def test_weekly_snapshot_decision_in_canonical_enum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#21 Phase 2 — `decision` must be one of the 7 canonical enum members."""
    target = _set_path(monkeypatch, tmp_path)
    canonical = {"HOLD", "DROP_5", "DROP_10", "DROP_15", "RAISE_5",
                 "ESCALATE_NON_PRICE", "INSUFFICIENT_DATA"}
    payload = _sample_weekly_snapshot_payload()
    snapshots.append_snapshot("weekly_snapshot", "287192992984", payload)
    row = json.loads(target.read_text().strip())
    assert row["decision"] in canonical


def test_weekly_snapshot_does_not_break_existing_event_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#21 Phase 2 — the existing 3 event types still work after enum extension."""
    target = _set_path(monkeypatch, tmp_path)
    snapshots.append_snapshot("analysis_baseline", "1", {"price_gbp": 10.0})
    snapshots.append_snapshot("price_change", "1", {"price_gbp": 9.0})
    snapshots.append_snapshot("post_change_check", "1", {"price_gbp": 9.0})
    snapshots.append_snapshot("weekly_snapshot", "1", _sample_weekly_snapshot_payload())
    rows = [json.loads(line) for line in target.read_text().strip().split("\n")]
    assert [r["event"] for r in rows] == [
        "analysis_baseline", "price_change", "post_change_check", "weekly_snapshot"
    ]
