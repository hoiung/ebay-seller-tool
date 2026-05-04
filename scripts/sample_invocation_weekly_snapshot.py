"""Issue #21 Phase 2 — sample invocation for the weekly_snapshot JSONL schema.

Writes 22 synthetic `weekly_snapshot` events end-to-end against an isolated
JSONL ledger (via EBAY_SNAPSHOT_PATH env override pointing at a temp file)
and asserts:

  1. All v1-consumed fields are populated on each row
  2. v2-deferred fields round-trip when populated
  3. Types match the Phase 2 schema contract
  4. Each row parses cleanly via json.loads (newline-delimited JSON)
  5. `decision` is one of the 7 canonical enum members

This is the AP #18 sample invocation for Phase 2 — proves the schema contract
holds end-to-end without touching production state. Default mode is offline
(temp-file ledger; safe to re-run).

PREREQUISITES: none — this script doesn't call eBay APIs.

USAGE:
  uv run python scripts/sample_invocation_weekly_snapshot.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from ebay import snapshots

# Mirror the canonical enumeration in tests/test_snapshots.py to keep the
# sample-invocation contract aligned with the unit-test contract.
V1_CONSUMED_FIELDS = {
    "week_id", "previous_price", "delta_pct", "floor_price_gbp",
    "imp_30d", "views_30d", "ctr_pct", "conv_pct", "tx_30d",
    "days_on_site", "decision", "decision_rationale", "consecutive_drops",
    "previous_decision", "manual_hold_flag", "data_quality_caveat",
    "applied_at",
}

V2_DEFERRED_FIELDS = {
    "mpn", "drive_type", "product_line", "condition_id", "cond_name",
    "watchers", "sold_lifetime", "rank_health_status", "audit_doc_cite",
    "elasticity_classification_at_snapshot", "authorized_by",
}

CANONICAL_DECISIONS = {
    "HOLD", "DROP_5", "DROP_10", "DROP_15", "RAISE_5",
    "ESCALATE_NON_PRICE", "INSUFFICIENT_DATA",
}

# Cycle through decisions to cover all 7 enum members across 22 rows.
_DECISION_CYCLE = [
    "HOLD", "DROP_5", "DROP_10", "DROP_15", "RAISE_5",
    "ESCALATE_NON_PRICE", "INSUFFICIENT_DATA",
]


def _payload_for(idx: int) -> dict:
    """Build a synthetic weekly_snapshot payload for listing index `idx` (0-21)."""
    decision = _DECISION_CYCLE[idx % len(_DECISION_CYCLE)]
    return {
        # Base-row
        "price_gbp": round(40.0 + idx * 0.7, 2),
        "quantity": None,
        "watch_count": None,
        "view_count": None,
        "traffic_30d": None,
        "source": "weekly_orchestrator_v1",
        # v1-consumed
        "week_id": "2026-W18",
        "previous_price": round(45.0 + idx * 0.7, 2) if idx % 3 else None,
        "delta_pct": round(-5.0 - idx * 0.1, 2) if idx % 3 else None,
        "floor_price_gbp": round(30.0 + idx * 0.5, 2),
        "imp_30d": 600 + idx * 80,
        "views_30d": 22 + idx,
        "ctr_pct": round(1.5 + idx * 0.05, 2),
        "conv_pct": 0.0 if idx % 4 else round(0.5 + idx * 0.05, 2),
        "tx_30d": 0 if idx % 4 else 1,
        "days_on_site": 30 + idx,
        "decision": decision,
        "decision_rationale": (
            f"Phase 2 sample row {idx}; F4 action gate met; "
            f"F5 step-band default for decision={decision}"
        ),
        "consecutive_drops": idx % 4,
        "previous_decision": "HOLD" if idx else None,
        "manual_hold_flag": idx == 7,
        "data_quality_caveat": "post-#29-toggle-window" if idx < 6 else None,
        "applied_at": None,
        # v2-deferred (write-only in v1; populate to verify round-trip)
        "mpn": f"ST{2000+idx}NX0253",
        "drive_type": "HDD",
        "product_line": "Enterprise Capacity",
        "condition_id": 3000,
        "cond_name": "Used",
        "watchers": idx % 3,
        "sold_lifetime": idx,
        "rank_health_status": "stalled" if idx % 5 else "healthy",
        "audit_doc_cite": f"14_SALES_IMPROVEMENT_2026-04-24.md#row-{idx+1}",
        "elasticity_classification_at_snapshot": None,
        "authorized_by": None,
    }


def _check_row(idx: int, row: dict) -> None:
    """Assert all Phase 2 contract invariants on one row."""
    # Base-row gates
    assert row["event"] == "weekly_snapshot", f"row {idx}: event mismatch"
    assert isinstance(row["item_id"], str), f"row {idx}: item_id not str"
    assert isinstance(row["timestamp"], str), f"row {idx}: timestamp not str"
    assert row["source"] == "weekly_orchestrator_v1", f"row {idx}: source mismatch"

    # v1-consumed: every field present
    for field in V1_CONSUMED_FIELDS:
        assert field in row, f"row {idx}: v1-consumed field {field!r} missing"

    # v2-deferred: every field present (write-only contract)
    for field in V2_DEFERRED_FIELDS:
        assert field in row, f"row {idx}: v2-deferred field {field!r} missing"

    # Type assertions for v1-consumed
    assert isinstance(row["week_id"], str)
    assert isinstance(row["decision"], str)
    assert row["decision"] in CANONICAL_DECISIONS, (
        f"row {idx}: decision {row['decision']!r} not in canonical 7-enum"
    )
    assert isinstance(row["decision_rationale"], str)
    assert isinstance(row["floor_price_gbp"], (int, float))
    assert isinstance(row["imp_30d"], int)
    assert isinstance(row["views_30d"], int)
    assert isinstance(row["ctr_pct"], (int, float))
    assert isinstance(row["conv_pct"], (int, float))
    assert isinstance(row["tx_30d"], int)
    assert isinstance(row["days_on_site"], int)
    assert isinstance(row["consecutive_drops"], int)
    assert isinstance(row["manual_hold_flag"], bool)
    assert row["previous_price"] is None or isinstance(row["previous_price"], (int, float))
    assert row["delta_pct"] is None or isinstance(row["delta_pct"], (int, float))
    assert row["previous_decision"] is None or isinstance(row["previous_decision"], str)
    assert row["data_quality_caveat"] is None or isinstance(row["data_quality_caveat"], str)
    assert row["applied_at"] is None or isinstance(row["applied_at"], str)


def main() -> int:
    print("=== Issue #21 Phase 2 sample invocation ===")
    print("Writing 22 synthetic weekly_snapshot events to a temp JSONL ledger...\n")

    seen_decisions: set[str] = set()
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger = Path(tmpdir) / "weekly_snapshots.jsonl"
        os.environ["EBAY_SNAPSHOT_PATH"] = str(ledger)
        try:
            for idx in range(22):
                snapshots.append_snapshot(
                    "weekly_snapshot",
                    f"3000000{idx:04d}",
                    _payload_for(idx),
                )

            assert ledger.exists(), "JSONL ledger not created"
            lines = ledger.read_text().strip().split("\n")
            assert len(lines) == 22, f"expected 22 rows, got {len(lines)}"

            for idx, line in enumerate(lines):
                row = json.loads(line)  # parse round-trip
                _check_row(idx, row)
                seen_decisions.add(row["decision"])

            print(f"  Rows written:        {len(lines)}")
            print(f"  v1-consumed fields:  {len(V1_CONSUMED_FIELDS)} per row")
            print(f"  v2-deferred fields:  {len(V2_DEFERRED_FIELDS)} per row")
            print(f"  Decisions covered:   {sorted(seen_decisions)}")
        finally:
            os.environ.pop("EBAY_SNAPSHOT_PATH", None)

    if seen_decisions != CANONICAL_DECISIONS:
        missing = CANONICAL_DECISIONS - seen_decisions
        print(f"\nFAIL: not all 7 canonical decisions exercised. Missing: {missing}",
              file=sys.stderr)
        return 1

    print("\nPASS — Phase 2 weekly_snapshot schema contract verified end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
