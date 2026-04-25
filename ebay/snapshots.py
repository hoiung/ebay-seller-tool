"""
Pricing-elasticity persistence (Issue #13 Phase 5).

Append-only JSONL at ~/.local/share/ebay-seller-tool/price_snapshots.jsonl.

Schema (per round-2 F-G template):
    {
        "timestamp": "<ISO 8601 UTC>",
        "item_id": "<eBay item id>",
        "event": "analysis_baseline | price_change | post_change_check | weekly_sweep",
        "price_gbp": <float | null>,
        "quantity": <int | null>,
        "watch_count": <int | null>,
        "view_count": <int | null>,
        "traffic_30d": <dict | null>,   # parse_traffic_report_response output if available
        "source": "<analyse_listing | update_listing | sweep | manual>"
    }

Newline-delimited; each line is one JSON object. Parsing tools (jq, pandas)
can stream the file without loading it whole.

`compute_elasticity` reads the file end-to-end (size grows by ~22 lines/week
per weekly sweep + handful of price_change events — manageable for years).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VALID_EVENT_TYPES = frozenset(
    {"analysis_baseline", "price_change", "post_change_check", "weekly_sweep"}
)

# Default snapshot path. Overridable via EBAY_SNAPSHOT_PATH env var (used by
# tests via monkeypatch).
_DEFAULT_PATH = Path.home() / ".local/share/ebay-seller-tool/price_snapshots.jsonl"


def _snapshot_path() -> Path:
    """Resolve snapshot path: env override beats default. Always returns absolute."""
    override = os.environ.get("EBAY_SNAPSHOT_PATH")
    if override:
        return Path(override)
    return _DEFAULT_PATH


def append_snapshot(event_type: str, item_id: str, snapshot: dict[str, Any]) -> None:
    """Append a snapshot event to the JSONL log.

    Creates parent directory + file if missing. Each write is flushed to disk
    so the file is durable on power loss (no buffered loss of the most recent
    event).

    Args:
        event_type: one of analysis_baseline / price_change / post_change_check /
            weekly_sweep. Raises ValueError on unknown.
        item_id: eBay item ID (string).
        snapshot: caller-provided fields to merge into the event row. The
            following keys take precedence over snapshot's: timestamp,
            item_id, event. All other keys passed through verbatim.

    Raises:
        ValueError: when event_type is not in the allowed enum.
    """
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(
            f"event_type must be one of {sorted(_VALID_EVENT_TYPES)}; got {event_type!r}"
        )
    if not item_id or not str(item_id).strip():
        raise ValueError("item_id required")

    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    row: dict[str, Any] = dict(snapshot)
    row["timestamp"] = datetime.now(timezone.utc).isoformat()
    row["item_id"] = str(item_id)
    row["event"] = event_type

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_events(item_id: str) -> list[dict[str, Any]]:
    """Read all events for one item_id from the JSONL log. Empty list if file missing."""
    path = _snapshot_path()
    if not path.exists():
        return []
    matches: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("item_id") == str(item_id):
                matches.append(row)
    return matches


def compute_elasticity(item_id: str, before_event: str, after_event: str) -> dict[str, Any] | None:
    """Compute pricing elasticity from snapshot events.

    Elasticity = Δwatchers / Δprice_pct, where Δprice_pct is signed.
    Classification (per round-2 F-G):
      - >2.0 absolute  → 'price_sensitive'
      - 0.5-2.0        → 'inconclusive'
      - <0.5 absolute  → 'inelastic'

    Args:
        item_id: eBay item ID.
        before_event: event_type to use as 'before' baseline.
        after_event: event_type to use as 'after' check.

    Returns:
        {
            "item_id": str,
            "before_event": str,
            "after_event": str,
            "before_price": float,
            "after_price": float,
            "before_watchers": int,
            "after_watchers": int,
            "delta_price_pct": float,
            "delta_watchers": int,
            "elasticity": float,
            "classification": "price_sensitive" | "inconclusive" | "inelastic",
        }
        or None if either event missing or before_price is 0 (avoid div-by-zero).
    """
    events = _read_events(item_id)
    before = next((e for e in events if e.get("event") == before_event), None)
    after = next((e for e in events if e.get("event") == after_event), None)
    if before is None or after is None:
        return None

    before_price = before.get("price_gbp")
    after_price = after.get("price_gbp")
    before_watchers = before.get("watch_count") or 0
    after_watchers = after.get("watch_count") or 0
    if before_price in (None, 0, 0.0):
        return None
    if after_price is None:
        return None

    delta_price_pct = round(100.0 * (after_price - before_price) / before_price, 2)
    delta_watchers = after_watchers - before_watchers

    if delta_price_pct == 0:
        elasticity = 0.0
    else:
        elasticity = round(delta_watchers / delta_price_pct, 2)

    abs_e = abs(elasticity)
    if abs_e > 2.0:
        classification = "price_sensitive"
    elif abs_e >= 0.5:
        classification = "inconclusive"
    else:
        classification = "inelastic"

    return {
        "item_id": str(item_id),
        "before_event": before_event,
        "after_event": after_event,
        "before_price": before_price,
        "after_price": after_price,
        "before_watchers": before_watchers,
        "after_watchers": after_watchers,
        "delta_price_pct": delta_price_pct,
        "delta_watchers": delta_watchers,
        "elasticity": elasticity,
        "classification": classification,
    }
