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
