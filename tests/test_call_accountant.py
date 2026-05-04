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


# ---- #21 Phase 1: account_call + sell_analytics namespace + RateLimitError ----


def test_call_caps_includes_sell_analytics() -> None:
    """#21 Phase 1 — sell_analytics quota tracked alongside Trading verbs."""
    assert ca.CALL_CAPS["sell_analytics"] == 9600


def test_account_call_records_namespace(isolated_state) -> None:
    """account_call increments the namespace counter independent of Trading verbs."""
    ca.account_call(api_namespace="sell_analytics")
    ca.account_call(api_namespace="sell_analytics")
    assert ca.today_count("sell_analytics") == 2


def test_account_call_namespace_isolation_from_trading_verbs(isolated_state) -> None:
    """#21 Phase 1 contract — Trading-API counters and Sell Analytics counters
    must increment independently. Mixing should NOT contaminate either bucket."""
    ca.record_call("AddMemberMessageRTQ")
    ca.record_call("AddMemberMessageRTQ")
    ca.account_call(api_namespace="sell_analytics")
    ca.account_call(api_namespace="sell_analytics")
    ca.account_call(api_namespace="sell_analytics")
    ca.record_call("GetMyeBaySelling")
    assert ca.today_count("AddMemberMessageRTQ") == 2
    assert ca.today_count("sell_analytics") == 3
    assert ca.today_count("GetMyeBaySelling") == 1


def test_account_call_raises_rate_limit_when_quota_exceeded(isolated_state) -> None:
    """#21 Phase 1 — RateLimitError fires BEFORE record_call when remaining
    quota is below expected_calls. Error names the namespace + remaining + cap."""
    # Push the counter to within 1 of a tiny synthetic cap by overriding
    # CALL_CAPS via monkeypatching the attribute (clean revert via the
    # isolated_state fixture's tmp_path scoping).
    original = dict(ca.CALL_CAPS)
    try:
        ca.CALL_CAPS["test_ns"] = 2
        ca.account_call(api_namespace="test_ns")
        ca.account_call(api_namespace="test_ns")
        # Counter is now at 2; cap is 2; remaining = 0; expected = 1 → raise
        with pytest.raises(ca.RateLimitError) as exc_info:
            ca.account_call(api_namespace="test_ns")
        err = exc_info.value
        assert err.api_namespace == "test_ns"
        assert err.remaining == 0
        assert err.cap == 2
        assert err.expected_calls == 1
        assert "test_ns" in str(err)
        assert "remaining=0" in str(err)
        assert "cap=2" in str(err)
        # Counter must NOT have advanced past the cap (record_call is gated).
        assert ca.today_count("test_ns") == 2
    finally:
        ca.CALL_CAPS.clear()
        ca.CALL_CAPS.update(original)


def test_account_call_rate_limit_is_call_accountant_subclass() -> None:
    """RateLimitError is-a CallAccountantError — callers can catch the parent
    class to handle either lock failures OR quota exhaustion uniformly."""
    assert issubclass(ca.RateLimitError, ca.CallAccountantError)
    assert issubclass(ca.CallAccountantError, RuntimeError)


def test_account_call_expected_calls_param(isolated_state) -> None:
    """expected_calls=N gate fires when remaining < N (batched-call check)."""
    original = dict(ca.CALL_CAPS)
    try:
        ca.CALL_CAPS["test_ns"] = 5
        # No prior calls; remaining = 5. expected_calls=10 must trip the gate.
        with pytest.raises(ca.RateLimitError) as exc_info:
            ca.account_call(api_namespace="test_ns", expected_calls=10)
        assert exc_info.value.expected_calls == 10
        assert exc_info.value.remaining == 5
        # Counter unchanged — gate fired before record_call.
        assert ca.today_count("test_ns") == 0
    finally:
        ca.CALL_CAPS.clear()
        ca.CALL_CAPS.update(original)


def test_record_call_accepts_snake_case_namespace(isolated_state) -> None:
    """#21 Phase 1 — record_call regex now allows snake_case namespaces in
    addition to CamelCase Trading verbs. Leading underscore still rejected."""
    ca.record_call("sell_analytics")  # must not raise
    ca.record_call("some_other_namespace")  # must not raise
    with pytest.raises(ValueError, match="CamelCase"):
        ca.record_call("_sell_analytics")  # leading underscore still forbidden
