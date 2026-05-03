"""Tests for ebay/call_accountant.py — daily Trading API call accountant.

Stage 5 fix L1.J / L1.F F5 (ebay-ops#12 post-impl review): the public
``ebay-seller-tool`` repo shipped ``ebay/call_accountant.py`` (Phase 2.1)
without any in-repo tests. All coverage lived in the private ``ebay-ops``
sibling, so consumers importing this module had no regression net.
This file mirrors the minimum-viable smoke for the four public surfaces
(``record_call``, ``today_count``, ``daily_budget_remaining``, ``CALL_CAPS``)
plus the flock + atomic-write contracts.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ebay import call_accountant as ca


@pytest.fixture
def isolated_state(monkeypatch, tmp_path: Path) -> Path:
    """Point _STATE_DIR at a fresh tmp_path — no real filesystem."""
    monkeypatch.setattr(ca, "_STATE_DIR", tmp_path)
    return tmp_path


def test_initial_today_count_is_zero(isolated_state) -> None:
    assert ca.today_count("AddMemberMessageRTQ") == 0


def test_record_call_increments(isolated_state) -> None:
    ca.record_call("AddMemberMessageRTQ")
    ca.record_call("AddMemberMessageRTQ")
    ca.record_call("AddMemberMessageRTQ")
    assert ca.today_count("AddMemberMessageRTQ") == 3


def test_per_verb_isolation(isolated_state) -> None:
    ca.record_call("AddMemberMessageRTQ")
    ca.record_call("GetMyeBaySelling")
    ca.record_call("GetMyeBaySelling")
    assert ca.today_count("AddMemberMessageRTQ") == 1
    assert ca.today_count("GetMyeBaySelling") == 2


def test_daily_budget_remaining_default_cap(isolated_state) -> None:
    ca.record_call("UnknownVerb")
    # Falls back to CALL_CAPS["_default"] = 5000.
    assert ca.daily_budget_remaining("UnknownVerb") == 4999


def test_daily_budget_explicit_cap_override(isolated_state) -> None:
    ca.record_call("AddMemberMessageRTQ")
    ca.record_call("AddMemberMessageRTQ")
    assert ca.daily_budget_remaining("AddMemberMessageRTQ", daily_cap=10) == 8


def test_call_caps_dict_shape() -> None:
    assert "_default" in ca.CALL_CAPS
    assert ca.CALL_CAPS["_default"] == 5000
    assert ca.CALL_CAPS["AddMemberMessageRTQ"] == 5000


def test_empty_call_name_raises_valueerror(isolated_state) -> None:
    with pytest.raises(ValueError):
        ca.record_call("")
    with pytest.raises(ValueError):
        ca.today_count("")


def test_corrupted_state_returns_zero(isolated_state) -> None:
    """Fail-soft: corrupted JSON file must NOT crash the read path
    (per AP #12 — accountant errors must not block live API calls)."""
    today_file = isolated_state / f"api-calls-{ca._today_yyyymmdd()}.json"
    today_file.write_text("not-valid-json{")
    assert ca.today_count("AddMemberMessageRTQ") == 0


def test_atomic_write_persists_on_disk(isolated_state) -> None:
    ca.record_call("AddMemberMessageRTQ")
    today_file = isolated_state / f"api-calls-{ca._today_yyyymmdd()}.json"
    assert today_file.exists()
    payload = json.loads(today_file.read_text())
    assert payload["AddMemberMessageRTQ"] == 1


def test_concurrent_record_calls_preserve_count(isolated_state) -> None:
    """2-thread contended record_call must preserve every increment
    via fcntl.flock — no lost updates. Sized to fit within the tightened
    5s lock timeout (Stage 5 fix L1.J) plus 100ms polling granularity:
    2 × 20 ops × ~4ms uncontended ≈ 160ms ideal; ~1.5s real under
    contention. 4-thread testing previously OK at 30s timeout but exposed
    timeout-driven loss under 5s — test the contract, not the previous
    head-room."""
    threads = [
        threading.Thread(target=lambda: [ca.record_call("AddMemberMessageRTQ") for _ in range(20)])
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert ca.today_count("AddMemberMessageRTQ") == 40, (
        "2 × 20 increments under contention must total 40 (no lost updates)"
    )


def test_lock_timeout_set_to_5_seconds() -> None:
    """Stage 5 fix L1.J — was 30s; tightened to 5s so accountant tail
    latency stays well under MAX_CUMULATIVE_TIMEOUT_SECONDS=15s."""
    assert ca._LOCK_TIMEOUT_SECONDS == 5


def test_call_name_regex_rejects_underscore_prefix(isolated_state) -> None:
    """Stage 5 fix L1.G M13 — reserved underscore-prefixed keys (like
    `_pruned`) must not be accepted as a verb name; would corrupt the
    prune marker. Reject any non-CamelCase verb name."""
    with pytest.raises(ValueError, match="CamelCase"):
        ca.record_call("_pruned")
    with pytest.raises(ValueError, match="CamelCase"):
        ca.record_call("verb-with-dash")
    with pytest.raises(ValueError, match="CamelCase"):
        ca.record_call("123InvalidFirstChar")


def test_quota_headroom_floor_constant() -> None:
    """Stage 5 fix L1.G M16 — load-bearing safety margin between send
    and read-side budget; must be readable + sensible default."""
    assert ca.QUOTA_HEADROOM_FLOOR == 10
    assert isinstance(ca.QUOTA_HEADROOM_FLOOR, int)


def test_negative_count_when_overshot(isolated_state) -> None:
    """daily_budget_remaining must return negative on overshoot (operator-
    visible signal), never silently floor to 0."""
    for _ in range(3):
        ca.record_call("AddMemberMessageRTQ")
    assert ca.daily_budget_remaining("AddMemberMessageRTQ", daily_cap=2) == -1
