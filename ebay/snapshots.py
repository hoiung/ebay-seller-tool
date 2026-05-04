"""
Pricing-elasticity persistence (Issue #13 Phase 5; #21 Phase 2 adds weekly_snapshot).

Append-only JSONL at ~/.local/share/ebay-seller-tool/price_snapshots.jsonl.

Base event row (every event_type carries these — per round-2 F-G template):
    {
        "timestamp": "<ISO 8601 UTC>",
        "item_id": "<eBay item id>",
        "event": "<event_type literal — see _VALID_EVENT_TYPES>",
        "price_gbp": <float | null>,
        "quantity": <int | null>,
        "watch_count": <int | null>,
        "view_count": <int | null>,
        "traffic_30d": <dict | null>,   # parse_traffic_report_response output if available
        "source": "<analyse_listing | update_listing | manual | weekly_orchestrator_v1>"
    }

Event types (`_VALID_EVENT_TYPES`):
    - ``analysis_baseline`` — captured on every analyse_listing call (#13)
    - ``price_change`` — written atomically with each successful update_listing (#13)
    - ``post_change_check`` — captured 7 days after a price_change for elasticity
    - ``weekly_snapshot`` — written by the ebay-ops weekly pricing orchestrator
      (#21 Phase 2). One row per active listing per weekly run. Adds the
      classifier-input + decision-record fields enumerated below; keeps the
      base-row fields populated where applicable (price_gbp = current price;
      watch_count / view_count nullable; source = "weekly_orchestrator_v1").

`weekly_snapshot` extra fields — ★ = consumed by v1 classifier or markdown
report; unstarred = v2-deferred (write-only in v1, reserved so v2 cluster
classifier doesn't need a JSONL backfill):

    v1-consumed (★):
      ★ week_id              # ISO week identifier "YYYY-Www" (e.g. "2026-W18")
      ★ previous_price       # last apply price before this snapshot, or null
      ★ delta_pct            # signed % delta vs previous_price, or null
      ★ floor_price_gbp      # computed floor from fees.yaml
      ★ imp_30d              # impressions, last 30d (Sell Analytics)
      ★ views_30d            # views, last 30d (Sell Analytics)
      ★ ctr_pct              # click-through rate %, last 30d
      ★ conv_pct             # sales conversion %, last 30d
      ★ tx_30d               # transactions, last 30d
      ★ days_on_site         # days since listing first went live
      ★ decision             # 7-enum: HOLD | DROP_5 | DROP_10 | DROP_15 |
                             #          RAISE_5 | ESCALATE_NON_PRICE | INSUFFICIENT_DATA
      ★ decision_rationale   # short string citing F4/F5/F6/F8 audit doc
      ★ consecutive_drops    # death-spiral guard counter
      ★ previous_decision    # decision from prior weekly_snapshot, or null
      ★ manual_hold_flag     # bool — pricing_overrides.json manual_hold lookup
      ★ data_quality_caveat  # string or null — e.g. "post-#29-toggle-window"
      ★ applied_at           # null at snapshot-write; populated when
                             #   apply_proposals writes the matching price_change

    v2-deferred (write-only in v1; populated when available):
      mpn                              # part number for v2 cluster classifier
      drive_type                       # "HDD" / "SSD" / "NIC" / etc.
      product_line                     # "Enterprise Capacity" / "Exos" / ...
      condition_id                     # eBay condition ID (1000/1500/3000/...)
      cond_name                        # short name ("New" / "Used" / ...)
      watchers                         # eBay watcher count (sparse in v1)
      sold_lifetime                    # cumulative sold across all-time
      rank_health_status               # qualitative health flag for cluster cls
      audit_doc_cite                   # link/anchor in 14_SALES_IMPROVEMENT
      elasticity_classification_at_snapshot   # output of Phase 7 elasticity loop
      authorized_by                    # operator literal that approved the apply

Issue #14 Phase 4 cleanup: ``weekly_sweep`` was removed from the enum
(F-DEADENUM). The literal had no producer in v1; the cadence-driven
sweep was deferred per Stage 1 §6. When sweep automation is implemented,
re-add the literal alongside the producer in the same commit.

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

_VALID_EVENT_TYPES = frozenset({
    "analysis_baseline",
    "price_change",
    "post_change_check",
    # #21 Phase 2 — written by the ebay-ops weekly pricing orchestrator.
    # Field set enumerated in module docstring above (v1-consumed ★ +
    # v2-deferred). One row per active listing per weekly run.
    "weekly_snapshot",
})

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
        event_type: one of analysis_baseline / price_change / post_change_check.
            Raises ValueError on unknown.
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

    Pair selection: when an item has multiple events of the same type
    (e.g. multi-revise produces multiple ``post_change_check`` rows), the
    FIRST match in JSONL append order — i.e. the OLDEST — is selected for
    both ``before`` and ``after``. This pairs the earliest baseline with
    the earliest revise, which is the right contract for "did the very
    first raise affect demand?" but NOT for "what's the elasticity of the
    most recent revise?". For the latter, callers should pass an explicit
    event_type pair (e.g. a custom ``after_event``) or pre-filter the
    JSONL.

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
        # Issue #14 Phase 2 — surface timestamps so callers can apply a
        # freshness gate (e.g. immediate post_change_check captured ~3-5s
        # post-revise won't show settled watcher elasticity; warn under 7d).
        "before_timestamp": before.get("timestamp"),
        "after_timestamp": after.get("timestamp"),
    }
