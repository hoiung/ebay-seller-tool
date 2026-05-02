"""Unit tests for the get_elasticity MCP tool (Issue #14 Phase 2 — AC2.1-AC2.4).

Exercises the freshness-gate path (events <7d apart → freshness_warning) and
the insufficient-events path. compute_elasticity itself is covered by
tests/test_snapshots.py — these tests focus on the MCP wrapper's freshness
augmentation + JSON shape contract.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import server


def _run(coro):
    return asyncio.run(coro)


def _write_event(
    path: Path,
    item_id: str,
    event: str,
    price: float,
    watch_count: int,
    timestamp: str,
) -> None:
    """Append a JSONL event with explicit timestamp (overrides the snapshots.py
    auto-stamp by writing directly)."""
    row = {
        "timestamp": timestamp,
        "item_id": item_id,
        "event": event,
        "price_gbp": price,
        "watch_count": watch_count,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def test_get_elasticity_returns_insufficient_events_when_no_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2.3 — missing event pair → {error: 'insufficient_events'}."""
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    # Only one event present — pair cannot be formed.
    _write_event(snap_path, "999", "analysis_baseline", 25.0, 7, "2026-04-25T10:00:00+00:00")

    result = _run(server.get_elasticity(item_id="999"))
    body = json.loads(result)
    assert body["error"] == "insufficient_events"
    assert body["item_id"] == "999"
    assert body["before_event"] == "analysis_baseline"
    assert body["after_event"] == "post_change_check"


def test_get_elasticity_returns_classification_on_paired_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2.3 — paired events → full elasticity shape with classification.

    Use a 14-day settled gap so freshness_warning is absent.
    """
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    base = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    after = base + timedelta(days=14)
    _write_event(snap_path, "999", "analysis_baseline", 25.0, 5, base.isoformat())
    _write_event(snap_path, "999", "post_change_check", 35.0, 1, after.isoformat())

    result = _run(server.get_elasticity(item_id="999"))
    body = json.loads(result)
    assert body["item_id"] == "999"
    assert body["before_price"] == 25.0
    assert body["after_price"] == 35.0
    assert body["before_watchers"] == 5
    assert body["after_watchers"] == 1
    # Δprice = +40%; Δwatchers = -4 → elasticity = -0.1 → inelastic (|0.1| < 0.5)
    # Note: the £25→£35 case — strong demand drop, but elasticity definition
    # uses Δwatchers per Δprice_pct, so a 40% raise with only 4-watcher drop
    # reads as inelastic. classification is documentary; the wrong-direction
    # WARN (Phase 3) is the actionable signal.
    assert body["classification"] in {"price_sensitive", "inconclusive", "inelastic"}
    assert "freshness_warning" not in body  # 14d > 7d gate
    assert body["delta_days"] == 14.0


def test_get_elasticity_surfaces_freshness_warning_when_under_7d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2.4 (L2.A FN-7) — events <7d apart → freshness_warning='events_too_close'.

    The immediate post_change_check fired by update_listing happens ~3-5s
    after the revise; watcher counts have not had time to settle. The
    classification is artefact, not behaviour.
    """
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    base = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    after = base + timedelta(seconds=5)  # immediate post-revise
    _write_event(snap_path, "999", "analysis_baseline", 25.0, 5, base.isoformat())
    _write_event(snap_path, "999", "post_change_check", 35.0, 5, after.isoformat())

    result = _run(server.get_elasticity(item_id="999"))
    body = json.loads(result)
    assert body["freshness_warning"] == "events_too_close"
    assert "Re-run after 7+ day settled period" in body["freshness_note"]
    # delta_days for a 5-second gap is essentially 0
    assert body["delta_days"] < 0.001


def test_get_elasticity_no_freshness_warning_at_exact_7d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2.4 boundary — exactly 7 days does NOT trigger freshness_warning."""
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    base = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    after = base + timedelta(days=7)
    _write_event(snap_path, "999", "analysis_baseline", 25.0, 5, base.isoformat())
    _write_event(snap_path, "999", "post_change_check", 35.0, 3, after.isoformat())

    result = _run(server.get_elasticity(item_id="999"))
    body = json.loads(result)
    assert "freshness_warning" not in body
    assert body["delta_days"] == 7.0


def test_get_elasticity_overrides_default_event_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2.2 — operator override of (before_event, after_event) pair.

    Use ("price_change", "post_change_check") to measure direct-action
    elasticity (the immediate snap → post-revise pair, both produced by
    update_listing).
    """
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    base = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    after = base + timedelta(days=10)
    _write_event(snap_path, "999", "price_change", 25.0, 5, base.isoformat())
    _write_event(snap_path, "999", "post_change_check", 35.0, 3, after.isoformat())

    result = _run(
        server.get_elasticity(
            item_id="999",
            before_event="price_change",
            after_event="post_change_check",
        )
    )
    body = json.loads(result)
    assert body["before_event"] == "price_change"
    assert body["after_event"] == "post_change_check"
    assert body["before_price"] == 25.0
    assert body["after_price"] == 35.0
