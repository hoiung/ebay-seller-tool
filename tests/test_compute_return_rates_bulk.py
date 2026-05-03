"""Tests for the G-NEW-11 bulk return-rates MCP tool."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import server


def test_empty_item_ids_refused() -> None:
    raw = asyncio.run(server.compute_return_rates_bulk(item_ids=[]))
    body = json.loads(raw)
    assert "error" in body
    assert "non-empty" in body["error"]


def test_invalid_days_refused() -> None:
    raw = asyncio.run(server.compute_return_rates_bulk(item_ids=["123"], days=0))
    body = json.loads(raw)
    assert "error" in body
    raw = asyncio.run(server.compute_return_rates_bulk(item_ids=["123"], days=200))
    body = json.loads(raw)
    assert "error" in body


def test_happy_path_aggregates_per_item() -> None:
    """All items succeed → summary counts succeeded=total, failed=0."""

    async def fake_rest(item_id: str, days: int) -> dict:
        return {
            "return_rate_pct": 5.0,
            "sold_count": 10,
            "returned_count": 1,
        }

    with patch("server.rest_compute_return_rate", side_effect=fake_rest):
        raw = asyncio.run(server.compute_return_rates_bulk(item_ids=["a", "b", "c"], days=30))
    body = json.loads(raw)
    assert body["summary"]["total"] == 3
    assert body["summary"]["succeeded"] == 3
    assert body["summary"]["failed"] == 0
    assert body["summary"]["high_return_rate_count"] == 0
    assert set(body["results"].keys()) == {"a", "b", "c"}
    assert body["results"]["a"]["return_rate_pct"] == 5.0


def test_partial_failure_does_not_abort_batch() -> None:
    """One item raising → recorded as {error}, batch continues."""
    call_count = {"n": 0}

    async def fake_rest(item_id: str, days: int) -> dict:
        call_count["n"] += 1
        if item_id == "fail":
            raise RuntimeError("simulated downstream failure")
        return {"return_rate_pct": 8.0, "sold_count": 5, "returned_count": 0}

    with patch("server.rest_compute_return_rate", side_effect=fake_rest):
        raw = asyncio.run(server.compute_return_rates_bulk(item_ids=["a", "fail", "c"], days=30))
    body = json.loads(raw)
    assert body["summary"]["total"] == 3
    assert body["summary"]["succeeded"] == 2
    assert body["summary"]["failed"] == 1
    assert "error" in body["results"]["fail"]
    assert "simulated" in body["results"]["fail"]["error"]
    # The successful items are still present
    assert body["results"]["a"]["return_rate_pct"] == 8.0
    assert body["results"]["c"]["return_rate_pct"] == 8.0


def test_l14_short_circuit_on_systemic_failure() -> None:
    """L14 (Ralph deferred Opus) -- ≥3 observations + ≥50% identical errors -> abort.

    Simulates an OAuth/auth/rate-limit failure cascading. After 3 items fail
    with the same RuntimeError("OAuth 401 expired"), the loop short-circuits
    and the remaining 47 items are surfaced via `unprocessed_item_ids`
    instead of cascading silently.
    """

    async def fake_rest(item_id: str, days: int) -> dict:
        raise RuntimeError("OAuth 401 expired")

    item_ids = [f"id{i}" for i in range(50)]
    with patch("server.rest_compute_return_rate", side_effect=fake_rest):
        raw = asyncio.run(server.compute_return_rates_bulk(item_ids=item_ids, days=30))
    body = json.loads(raw)
    summary = body["summary"]

    # Short-circuit triggers at observed=3 (3/3=1.0 >= 0.5).
    assert summary["short_circuited"] is True
    assert summary["systemic_error_class"] == "RuntimeError"
    assert summary["systemic_error_message"] == "OAuth 401 expired"
    assert summary["systemic_error_count"] == 3
    assert summary["processed"] == 3
    assert summary["failed"] == 3
    assert summary["succeeded"] == 0

    # 47 items unprocessed
    unprocessed = summary["unprocessed_item_ids"]
    assert len(unprocessed) == 47
    assert unprocessed[0] == "id3"
    assert unprocessed[-1] == "id49"
    # Every recorded result has both error + error_class
    for entry in body["results"].values():
        assert "error_class" in entry
        assert entry["error_class"] == "RuntimeError"


def test_l14_no_short_circuit_below_threshold() -> None:
    """L14 (Ralph deferred Opus) -- only 1/3 failed = 0.33, no short-circuit."""

    async def fake_rest(item_id: str, days: int) -> dict:
        if item_id == "fail":
            raise RuntimeError("transient blip")
        return {"return_rate_pct": 2.0, "sold_count": 8, "returned_count": 0}

    with patch("server.rest_compute_return_rate", side_effect=fake_rest):
        raw = asyncio.run(server.compute_return_rates_bulk(item_ids=["a", "fail", "c"], days=30))
    body = json.loads(raw)
    summary = body["summary"]
    # No short-circuit: 1/3 < 0.5
    assert "short_circuited" not in summary
    assert summary["processed"] == 3
    assert summary["succeeded"] == 2
    assert summary["failed"] == 1


def test_l14_diverse_errors_dont_short_circuit() -> None:
    """L14 (Ralph deferred Opus) -- 3 different error classes -> no signature majority."""

    async def fake_rest(item_id: str, days: int) -> dict:
        if item_id == "a":
            raise RuntimeError("err A")
        if item_id == "b":
            raise ValueError("err B")
        if item_id == "c":
            raise KeyError("err C")
        return {"return_rate_pct": 1.0, "sold_count": 1, "returned_count": 0}

    with patch("server.rest_compute_return_rate", side_effect=fake_rest):
        raw = asyncio.run(server.compute_return_rates_bulk(item_ids=["a", "b", "c", "d"], days=30))
    body = json.loads(raw)
    summary = body["summary"]
    # 3 distinct signatures over 3 observations -> top_count=1, 1/3 < 0.5.
    assert "short_circuited" not in summary
    assert summary["failed"] == 3
    assert summary["succeeded"] == 1


def test_high_return_rate_count_flags_above_threshold() -> None:
    """>15% return rate → counted in high_return_rate_count for floor-price override."""

    async def fake_rest(item_id: str, days: int) -> dict:
        rates = {"a": 5.0, "b": 18.0, "c": 25.0, "d": 15.0}
        return {
            "return_rate_pct": rates[item_id],
            "sold_count": 10,
            "returned_count": 1,
        }

    with patch("server.rest_compute_return_rate", side_effect=fake_rest):
        raw = asyncio.run(server.compute_return_rates_bulk(item_ids=["a", "b", "c", "d"], days=90))
    body = json.loads(raw)
    # 18% and 25% > 15.0; 15.0 is NOT strictly greater
    assert body["summary"]["high_return_rate_count"] == 2
    assert body["summary"]["high_return_rate_threshold_pct"] == 15.0
