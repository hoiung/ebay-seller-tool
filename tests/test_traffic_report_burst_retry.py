"""Issue #31 Phase 1 — burst-rate-limit retry tests for fetch_traffic_report.

Covers the four invariants from the issue's acceptance shape:
  * 429-then-recover (one 429 then 200 — single retry succeeds)
  * 429-after-budget-exhausted (every attempt 429s — surfaces TrafficReportRateLimitError)
  * non-429 errors fall through immediately (HTTP 500, network drop) — burst
    retry must not mask different root causes
  * burst-window tracker on call_accountant emits the warn log when
    Sell Analytics call frequency crosses the empirical threshold

Tests inject sleep_fn + monotonic_fn into the sync retry helper so the
loop drives without burning real seconds. The async fetch_traffic_report
entry point is exercised separately via a fake _sync helper to confirm
the public surface is wired correctly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from ebay import call_accountant, rest
from ebay.rest import (
    TrafficReportRateLimitError,
    _sync_get_traffic_report_with_retry,
)


def _run(coro):
    return asyncio.run(coro)


def _make_clock(start: float = 1000.0):
    """Stub monotonic clock + sleep that advances it. Returns
    (sleep_fn, monotonic_fn, get_total_advance)."""
    state = {"now": start, "total_slept": 0.0}

    def sleep_fn(seconds: float) -> None:
        state["now"] += seconds
        state["total_slept"] += seconds

    def monotonic_fn() -> float:
        return state["now"]

    def total_slept() -> float:
        return state["total_slept"]

    return sleep_fn, monotonic_fn, total_slept


def _rate_limited_exc() -> PermissionError:
    return PermissionError(
        "eBay API 429 on https://api.ebay.com/sell/analytics/v1/traffic_report: rate limited"
    )


def test_429_then_recover_returns_payload_in_one_retry() -> None:
    """First call raises 429, second returns payload. Wrapper should return
    the payload and account for exactly one backoff sleep."""
    sleep_fn, monotonic_fn, total_slept = _make_clock()
    fake_payload = {"header": {"metrics": []}, "records": []}
    call_count = {"n": 0}

    def fake_sync(_ids, _days, _market):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _rate_limited_exc()
        return fake_payload

    with patch.object(rest, "_sync_get_traffic_report", side_effect=fake_sync):
        result = _sync_get_traffic_report_with_retry(
            ["111"],
            30,
            "EBAY_GB",
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
    assert result is fake_payload
    assert call_count["n"] == 2, "second call should succeed"
    # 5s backoff for the first retry (matches _BURST_RETRY_BACKOFF_SECONDS[0])
    assert total_slept() == 5.0


def test_429_then_recover_on_third_attempt() -> None:
    """First two calls 429, third succeeds. Two backoffs (5s + 15s)."""
    sleep_fn, monotonic_fn, total_slept = _make_clock()
    fake_payload = {"header": {"metrics": []}, "records": []}
    call_count = {"n": 0}

    def fake_sync(_ids, _days, _market):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise _rate_limited_exc()
        return fake_payload

    with patch.object(rest, "_sync_get_traffic_report", side_effect=fake_sync):
        result = _sync_get_traffic_report_with_retry(
            ["111"],
            30,
            "EBAY_GB",
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
    assert result is fake_payload
    assert call_count["n"] == 3
    assert total_slept() == 5.0 + 15.0


def test_429_budget_exhausted_raises_rate_limit_error() -> None:
    """Every attempt 429s. After the configured backoff sequence the wrapper
    raises TrafficReportRateLimitError carrying attempts + total_wait + last_error."""
    sleep_fn, monotonic_fn, total_slept = _make_clock()
    call_count = {"n": 0}

    def fake_sync(_ids, _days, _market):
        call_count["n"] += 1
        raise _rate_limited_exc()

    with patch.object(rest, "_sync_get_traffic_report", side_effect=fake_sync):
        with pytest.raises(TrafficReportRateLimitError) as exc_info:
            _sync_get_traffic_report_with_retry(
                ["111"],
                30,
                "EBAY_GB",
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )

    err = exc_info.value
    # Default schedule: 1 initial attempt + 3 retries = 4 attempts.
    assert err.attempts == 4
    assert err.total_wait_seconds == 5.0 + 15.0 + 60.0
    assert "eBay API 429" in err.last_error
    assert call_count["n"] == 4
    assert total_slept() == err.total_wait_seconds


def test_429_total_budget_caps_long_backoff() -> None:
    """If the configured backoff would exceed total_budget_seconds, the
    final sleep is clamped to the remaining budget rather than overshooting.
    Verifies the wall-clock budget is the real ceiling, not the schedule."""
    sleep_fn, monotonic_fn, total_slept = _make_clock()

    def fake_sync(_ids, _days, _market):
        raise _rate_limited_exc()

    with patch.object(rest, "_sync_get_traffic_report", side_effect=fake_sync):
        with pytest.raises(TrafficReportRateLimitError) as exc_info:
            _sync_get_traffic_report_with_retry(
                ["111"],
                30,
                "EBAY_GB",
                backoff_seconds=(10.0, 30.0, 100.0),  # 3rd retry would overshoot
                total_budget_seconds=25.0,
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )

    # First retry sleeps 10s (10s elapsed); second retry would sleep 30 but
    # remaining budget is 15s — clamps to 15. Third retry has 0 budget,
    # loop exits without sleeping further.
    assert total_slept() == 25.0
    assert exc_info.value.total_wait_seconds == 25.0


def test_non_429_error_is_not_retried() -> None:
    """A 500 or 401 (non-429) PermissionError must propagate on the FIRST
    attempt — burst retry only handles 429."""
    sleep_fn, monotonic_fn, total_slept = _make_clock()
    call_count = {"n": 0}

    def fake_sync(_ids, _days, _market):
        call_count["n"] += 1
        raise PermissionError("eBay API 500 on https://api.ebay.com/...: server error")

    with patch.object(rest, "_sync_get_traffic_report", side_effect=fake_sync):
        with pytest.raises(PermissionError, match="eBay API 500"):
            _sync_get_traffic_report_with_retry(
                ["111"],
                30,
                "EBAY_GB",
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )
    assert call_count["n"] == 1, "non-429 must not be retried"
    assert total_slept() == 0.0


def test_unrelated_exception_is_not_retried() -> None:
    """A network-level exception (ConnectionError, ValueError) is NOT a
    rate-limit signal and must propagate immediately."""
    sleep_fn, monotonic_fn, total_slept = _make_clock()
    call_count = {"n": 0}

    def fake_sync(_ids, _days, _market):
        call_count["n"] += 1
        raise ConnectionError("DNS resolution failed")

    with patch.object(rest, "_sync_get_traffic_report", side_effect=fake_sync):
        with pytest.raises(ConnectionError, match="DNS resolution failed"):
            _sync_get_traffic_report_with_retry(
                ["111"],
                30,
                "EBAY_GB",
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )
    assert call_count["n"] == 1
    assert total_slept() == 0.0


def test_rate_limit_message_regex_matches_only_429() -> None:
    """Sanity guard on the 429-detection regex — anchored at start, word
    boundary after to avoid eg. 4290 collision."""
    from ebay.rest import _is_rate_limited_error

    assert _is_rate_limited_error(PermissionError("eBay API 429 on x: y"))
    assert _is_rate_limited_error(PermissionError("eBay API 429 on https://...: rate"))
    assert not _is_rate_limited_error(PermissionError("eBay API 4290 on x: y"))
    assert not _is_rate_limited_error(PermissionError("eBay API 401 on x: y"))
    assert not _is_rate_limited_error(PermissionError("eBay API 500 on x: y"))
    assert not _is_rate_limited_error(RuntimeError("eBay API 429 on x: y"))  # wrong type
    assert not _is_rate_limited_error(ValueError("not an ebay error"))


def test_async_fetch_traffic_report_propagates_through_to_thread() -> None:
    """Public async surface fetch_traffic_report wires through to the retry
    helper. Patches the sync helper so the public path is verified end-to-end
    without hitting eBay (or the call_accountant ledger)."""
    fake_payload = {"header": {"metrics": []}, "records": []}

    def fake_sync_with_retry(ids, days, marketplace, **_kwargs):
        # The async wrapper passes positional listing_ids, days, marketplace.
        assert ids == ["111", "222"]
        assert days == 30
        assert marketplace == "EBAY_GB"
        return fake_payload

    with (
        patch("ebay.call_accountant.account_call"),  # bypass quota gate
        patch.object(rest, "_sync_get_traffic_report_with_retry", side_effect=fake_sync_with_retry),
    ):
        result = _run(rest.fetch_traffic_report(["111", "222"], days=30))
    assert result is fake_payload


def test_burst_window_tracker_emits_warn_log() -> None:
    """call_accountant._record_burst_call should log a burst_window_warn line
    once threshold+1 calls fire inside the configured window. Below threshold
    no warn fires."""
    call_accountant.reset_burst_tracker()
    window_seconds, threshold = call_accountant.BURST_WINDOWS["sell_analytics"]
    assert threshold >= 2, "test relies on threshold >= 2"

    captured: list[str] = []

    def capture(line: str) -> None:
        captured.append(line)

    # threshold-1 calls within window — no warn
    with patch("ebay.client.log_debug", side_effect=capture):
        for _ in range(threshold - 1):
            call_accountant._record_burst_call("sell_analytics", now=1000.0)
    assert not any("burst_window_warn" in line for line in captured)

    # one more call (= threshold total) inside the window — warn fires once
    with patch("ebay.client.log_debug", side_effect=capture):
        call_accountant._record_burst_call("sell_analytics", now=1000.5)
    assert any("burst_window_warn" in line for line in captured), captured

    # within cooldown — additional calls do not re-warn
    pre_count = sum(1 for line in captured if "burst_window_warn" in line)
    with patch("ebay.client.log_debug", side_effect=capture):
        for _ in range(5):
            call_accountant._record_burst_call("sell_analytics", now=1001.0)
    post_count = sum(1 for line in captured if "burst_window_warn" in line)
    assert post_count == pre_count, "warn cooldown must suppress duplicates"

    # after cooldown elapses + still bursting — new warn allowed
    later = 1000.0 + call_accountant._BURST_WARN_COOLDOWN_SECONDS + 1.0
    with patch("ebay.client.log_debug", side_effect=capture):
        # advance clock past window so old timestamps drop, then re-burst
        for i in range(threshold + 1):
            call_accountant._record_burst_call("sell_analytics", now=later + i * 0.1)
    final_count = sum(1 for line in captured if "burst_window_warn" in line)
    assert final_count > post_count, "post-cooldown burst should re-warn"

    call_accountant.reset_burst_tracker()


def test_burst_window_tracker_skips_unconfigured_namespace() -> None:
    """Namespaces without a BURST_WINDOWS entry incur zero tracking overhead
    (defensive — keeps Trading verb hot-path free of new state)."""
    call_accountant.reset_burst_tracker()
    captured: list[str] = []
    with patch("ebay.client.log_debug", side_effect=captured.append):
        for _ in range(50):
            call_accountant._record_burst_call("AddMemberMessageRTQ", now=1000.0)
    assert not any("burst_window_warn" in line for line in captured)
    assert "AddMemberMessageRTQ" not in call_accountant._BURST_RECENT
