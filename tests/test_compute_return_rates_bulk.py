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
        raw = asyncio.run(
            server.compute_return_rates_bulk(item_ids=["a", "b", "c"], days=30)
        )
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
        raw = asyncio.run(
            server.compute_return_rates_bulk(item_ids=["a", "fail", "c"], days=30)
        )
    body = json.loads(raw)
    assert body["summary"]["total"] == 3
    assert body["summary"]["succeeded"] == 2
    assert body["summary"]["failed"] == 1
    assert "error" in body["results"]["fail"]
    assert "simulated" in body["results"]["fail"]["error"]
    # The successful items are still present
    assert body["results"]["a"]["return_rate_pct"] == 8.0
    assert body["results"]["c"]["return_rate_pct"] == 8.0


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
        raw = asyncio.run(
            server.compute_return_rates_bulk(item_ids=["a", "b", "c", "d"], days=90)
        )
    body = json.loads(raw)
    # 18% and 25% > 15.0; 15.0 is NOT strictly greater
    assert body["summary"]["high_return_rate_count"] == 2
    assert body["summary"]["high_return_rate_threshold_pct"] == 15.0
