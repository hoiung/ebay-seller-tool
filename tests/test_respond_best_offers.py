"""Integration tests for respond_best_offers.py poller (Issue #16 AC3.7).

The script lives in the sibling ebay-ops repo at
~/DevProjects/ebay-ops/.claude/skills/ebay-seller-tool/scripts/respond_best_offers.py
— same sys.path bootstrap pattern as enable_best_offer_all.py (which has
no test today; #16 Phase 3 sets the precedent).

9 tests covering:
- 3-tier decision dispatch (accept / counter / decline)
- idempotency skip
- disable-flag blocks all
- per-offer error isolation (3 sub-cases: transport error, eBay validation
  error 21916 BestOffer-disabled, live_price=0 catastrophic-accept guard)
- partial JSONL last-line tolerance
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

# ebay-ops scripts dir → sys.path so we can import respond_best_offers.
# The responder lives in the PRIVATE sibling ebay-ops repo. On a clean CI
# runner that repo is not cloned, so collection-time import would crash and
# fail the whole pytest run. Skip the module cleanly when the script is
# absent — local dev keeps full coverage; CI gets a clean skip.
_RESPONDER_DIR = (
    Path.home() / "DevProjects" / "ebay-ops" / ".claude" / "skills" / "ebay-seller-tool" / "scripts"
)
_RESPONDER_PATH = _RESPONDER_DIR / "respond_best_offers.py"
if not _RESPONDER_PATH.exists():
    pytest.skip(
        f"respond_best_offers.py not found at {_RESPONDER_PATH} "
        "(private ebay-ops repo not cloned — expected on CI runner)",
        allow_module_level=True,
    )

if str(_RESPONDER_DIR) not in sys.path:
    sys.path.insert(0, str(_RESPONDER_DIR))

import respond_best_offers as rbo  # noqa: E402

from ebay.fees import reset_fees_cache  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_fees_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Synthetic fees.yaml + EBAY_FEES_CONFIG redirect (mirrors
    test_compute_best_offer_thresholds.py)."""
    config_path = tmp_path / "fees.yaml"
    monkeypatch.setenv("EBAY_FEES_CONFIG", str(config_path))
    base = {
        "ebay_uk": {
            "fvf_rate": 0.1548,
            "per_order_fee_gbp": 0.40,
            "marketplace_id": "EBAY_GB",
            "site_id": 3,
        },
        "postage": {"outbound_gbp": 3.50, "return_gbp": 3.50},
        "packaging_gbp": 0.60,
        "time_cost": {
            "mode": "sunk",
            "sale_gbp": 0.0,
            "return_gbp": 0.0,
            "hourly_rate_gbp": 30.0,
        },
        "defaults": {"cogs_gbp": 0.0, "return_rate": 0.10, "target_margin": 0.15},
        "under_pricing": {
            "velocity_median_default": 0.1,
            "recommended_band_low_pct": 40,
            "recommended_band_high_pct": 55,
        },
        "outlier_rejection": {
            "enabled": True,
            "method": "iqr",
            "multiplier": 1.5,
            "log_transform": True,
            "min_pool_size": 6,
            "max_drop_frac": 0.20,
            "per_condition": False,
        },
        "best_offer": {
            "auto_accept_pct": 0.925,
            "auto_decline_pct": 0.75,
            "counter_offer_pct": 0.95,
            "round_down_to_pound": True,
        },
    }
    config_path.write_text(yaml.safe_dump(base))
    reset_fees_cache()
    yield base
    reset_fees_cache()


@pytest.fixture
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect LEDGER_DIR to tmp_path so tests don't touch the real ledger."""
    monkeypatch.setattr(rbo, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(rbo, "LEDGER_PATH", tmp_path / "ledger" / "best_offer_responses.jsonl")
    monkeypatch.setattr(rbo, "DISABLE_FLAG_PATH", tmp_path / "ledger" / ".disable_auto_counter")
    return tmp_path / "ledger"


def _read_ledger(ledger_dir: Path) -> list[dict]:
    """Helper to read JSONL rows from the test-isolated ledger."""
    path = ledger_dir / "best_offer_responses.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _build_offer(
    *,
    offer_id: str = "o1",
    item_id: str = "287260458724",
    buyer_offer_gbp: float = 45.0,
    buyer_user_id: str = "buyer_uk",
    offer_timestamp_iso: str = "2026-05-02T14:30:00Z",
) -> dict:
    return {
        "offer_id": offer_id,
        "item_id": item_id,
        "buyer_user_id": buyer_user_id,
        "buyer_offer_gbp": buyer_offer_gbp,
        "buyer_message": "",
        "offer_timestamp_iso": offer_timestamp_iso,
        "expiration_iso": "2026-05-04T14:30:00Z",
        "best_offer_code_type": "ManualBestOffer",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_responder_accepts_offer_above_auto_accept_threshold(
    isolated_fees_config, isolated_ledger
) -> None:
    """Live £50, offer £47 → 47 ≥ floor(0.925*50)=46 → Accept dispatched."""
    pending = [_build_offer(buyer_offer_gbp=47.0)]

    accept_mock = AsyncMock(
        return_value={
            "success": True,
            "ebay_response_status": "Success",
            "ebay_response_code": None,
            "error_message": None,
        }
    )
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=pending)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", accept_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 0
    assert accept_mock.await_count == 1
    assert accept_mock.await_args.kwargs["action"] == "Accept"  # AP #18 explicit
    assert accept_mock.await_args.kwargs["item_id"] == "287260458724"

    rows = _read_ledger(isolated_ledger)
    assert len(rows) == 1
    assert rows[0]["cron_action"] == "accept"


def test_responder_counters_offer_in_band(isolated_fees_config, isolated_ledger) -> None:
    """Live £50, offer £40 → 40 in [floor(0.75*50)=37, floor(0.925*50)=46)
    → Counter at floor(0.95*50)=47."""
    pending = [_build_offer(buyer_offer_gbp=40.0)]

    counter_mock = AsyncMock(
        return_value={
            "success": True,
            "ebay_response_status": "Success",
            "ebay_response_code": None,
            "error_message": None,
        }
    )
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=pending)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", counter_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 0
    assert counter_mock.await_args.kwargs["action"] == "Counter"
    assert counter_mock.await_args.kwargs["counter_price_gbp"] == 47.0  # floor(0.95*50)

    rows = _read_ledger(isolated_ledger)
    assert rows[0]["cron_action"] == "counter"
    assert rows[0]["counter_price_gbp"] == 47


def test_responder_declines_offer_below_auto_decline_threshold(
    isolated_fees_config, isolated_ledger
) -> None:
    """Live £50, offer £30 → 30 < floor(0.75*50)=37 → Decline."""
    pending = [_build_offer(buyer_offer_gbp=30.0)]

    decline_mock = AsyncMock(
        return_value={
            "success": True,
            "ebay_response_status": "Success",
            "ebay_response_code": None,
            "error_message": None,
        }
    )
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=pending)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", decline_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 0
    assert decline_mock.await_args.kwargs["action"] == "Decline"

    rows = _read_ledger(isolated_ledger)
    assert rows[0]["cron_action"] == "decline"


def test_responder_idempotency_skips_already_responded_offer(
    isolated_fees_config, isolated_ledger
) -> None:
    """Run twice with same pending offer → second run logs skip:already_responded."""
    pending = [_build_offer(buyer_offer_gbp=47.0)]
    accept_mock = AsyncMock(
        return_value={
            "success": True,
            "ebay_response_status": "Success",
            "ebay_response_code": None,
            "error_message": None,
        }
    )
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=pending)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", accept_mock),
    ):
        rbo.main(["--apply", "--yes"])
        rbo.main(["--apply", "--yes"])  # same pending

    assert accept_mock.await_count == 1  # ONLY first run dispatches Accept
    rows = _read_ledger(isolated_ledger)
    assert len(rows) == 2
    assert rows[0]["cron_action"] == "accept"
    assert rows[1]["cron_action"] == "skip"
    assert rows[1]["reason"] == "already_responded_state_match"


def test_responder_disable_flag_blocks_all_actions(isolated_fees_config, isolated_ledger) -> None:
    """Touch disable flag → exit 0, JSONL has single skip row, no API calls fired."""
    isolated_ledger.mkdir(parents=True, exist_ok=True)
    rbo.DISABLE_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rbo.DISABLE_FLAG_PATH.touch()

    accept_mock = AsyncMock()
    poll_mock = AsyncMock()
    with (
        patch.object(rbo, "get_pending_best_offers", poll_mock),
        patch.object(rbo, "respond_to_best_offer", accept_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 0
    assert poll_mock.await_count == 0  # never polled — disabled BEFORE API
    assert accept_mock.await_count == 0
    rows = _read_ledger(isolated_ledger)
    assert len(rows) == 1
    assert rows[0]["reason"] == "disabled_by_flag"
    assert rows[0]["cron_action"] == "skip"


def test_responder_per_offer_error_does_not_cascade(isolated_fees_config, isolated_ledger) -> None:
    """3 pending offers — middle one raises ConnectionError; others must succeed."""
    o1 = _build_offer(offer_id="o1", buyer_offer_gbp=47.0)
    o2 = _build_offer(offer_id="o2", buyer_offer_gbp=47.0)
    o3 = _build_offer(offer_id="o3", buyer_offer_gbp=47.0)

    async def respond_side_effect(item_id: str, offer_id: str, action: str, **kwargs):
        if offer_id == "o2":
            raise ConnectionError("transient network blip")
        return {
            "success": True,
            "ebay_response_status": "Success",
            "ebay_response_code": None,
            "error_message": None,
        }

    respond_mock = AsyncMock(side_effect=respond_side_effect)
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=[o1, o2, o3])),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", respond_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 0  # transient is not auth-expiry, no exit 1
    assert respond_mock.await_count == 3  # all 3 attempted, none cascaded
    rows = _read_ledger(isolated_ledger)
    assert len(rows) == 3
    actions = [r["cron_action"] for r in rows]
    assert actions.count("accept") == 2
    assert actions.count("error") == 1
    err_row = next(r for r in rows if r["cron_action"] == "error")
    assert err_row["offer_id"] == "o2"
    assert "ConnectionError" in err_row["error_message"]


def test_responder_skips_offer_when_live_price_is_zero(
    isolated_fees_config, isolated_ledger
) -> None:
    """live_price=0 (snapshot fail) → skip + JSONL row, NO RespondToBestOffer dispatched
    (catastrophic-accept guard — auto_accept = floor(0.925 * 0) = 0 would otherwise
    accept any non-negative offer)."""
    pending = [_build_offer(item_id="ghost", buyer_offer_gbp=47.0)]
    accept_mock = AsyncMock()
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=pending)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={}),
        patch.object(rbo, "respond_to_best_offer", accept_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 0
    assert accept_mock.await_count == 0  # never dispatched
    rows = _read_ledger(isolated_ledger)
    assert rows[0]["cron_action"] == "skip"
    assert rows[0]["reason"] == "live_price_zero_or_missing"


def test_responder_skips_listing_with_best_offer_disabled_error(
    isolated_fees_config, isolated_ledger
) -> None:
    """eBay returns Failure ack with code 21916 → caught + JSONL'd, loop continues.

    Simulates operator toggling Best Offer OFF on a listing mid-month between
    poll + dispatch — the responder must not cascade.
    """
    o1 = _build_offer(offer_id="o1", buyer_offer_gbp=47.0)
    o2 = _build_offer(offer_id="o2", buyer_offer_gbp=47.0)

    async def respond_side_effect(item_id: str, offer_id: str, action: str, **kwargs):
        if offer_id == "o1":
            return {
                "success": False,
                "ebay_response_status": "Failure",
                "ebay_response_code": "21916",
                "error_message": "Best Offer not available on this listing",
            }
        return {
            "success": True,
            "ebay_response_status": "Success",
            "ebay_response_code": None,
            "error_message": None,
        }

    respond_mock = AsyncMock(side_effect=respond_side_effect)
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=[o1, o2])),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", respond_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 0
    assert respond_mock.await_count == 2  # both attempted, no cascade
    rows = _read_ledger(isolated_ledger)
    assert len(rows) == 2
    err_row = next(r for r in rows if r["offer_id"] == "o1")
    assert err_row["cron_action"] == "error"
    assert "21916" in (err_row["error_message"] or "")
    ok_row = next(r for r in rows if r["offer_id"] == "o2")
    assert ok_row["cron_action"] == "accept"


def test_responder_load_jsonl_tail_skips_partial_last_line(
    isolated_fees_config, isolated_ledger
) -> None:
    """Truncated last JSONL line (SIGTERM/OOM during fsync) → JSONDecodeError caught,
    log_warn + continue. Idempotency check completes against the valid prefix."""
    isolated_ledger.mkdir(parents=True, exist_ok=True)
    # Write 1 valid row + 1 truncated row
    valid_row = {
        "timestamp": "2026-05-02T14:30:00Z",
        "offer_id": "valid_offer",
        "state_hash": "abc123",
        "item_id": "287260458724",
        "buyer_user_id": "buyer_uk",
        "buyer_offer_gbp": 47.0,
        "live_price_gbp": 50.0,
        "cron_action": "accept",
        "counter_price_gbp": None,
        "reason": "ok",
        "error_message": None,
    }
    rbo.LEDGER_PATH.write_text(
        json.dumps(valid_row) + '\n{"timestamp":"2026-05-02T14:35:00Z","offer_id":"trunc'
    )

    # load_recent_signatures must skip the partial line, return only valid_offer
    seen = rbo.load_recent_signatures(window_hours=10000)
    assert ("valid_offer", "abc123") in seen
    assert len(seen) == 1  # truncated row not parsed


def test_responder_aborts_when_offer_count_exceeds_sanity_cap(
    isolated_fees_config, isolated_ledger
) -> None:
    """Stage 5 gap-fill: malformed GetBestOffers response with >SANITY_OFFER_CAP
    offers must abort the cycle (return exit 1) rather than process all of them.
    Defensive guard against a runaway eBay response."""
    bloated = [_build_offer(offer_id=f"o{i}") for i in range(rbo.SANITY_OFFER_CAP + 1)]
    respond_mock = AsyncMock()
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=bloated)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", respond_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 1
    assert respond_mock.await_count == 0  # no per-offer dispatch on abort


def test_responder_apply_without_yes_refuses_live_mode() -> None:
    """Stage 5 gap-fill: --apply without --yes must refuse with exit 1
    and never reach the async main_async (no eBay API calls).
    Mirrors the enable_best_offer_all.py CLI safety pattern."""
    with patch.object(rbo, "main_async", AsyncMock()) as main_async_mock:
        exit_code = rbo.main(["--apply"])
    assert exit_code == 1
    assert main_async_mock.await_count == 0


def test_compute_decision_declines_when_counter_would_exceed_live_price() -> None:
    """Stage 5 fix: when `floor_gbp >= live_price` (high-postage / small-margin
    listings — e.g. £10 listing with computed £11 floor), countering at
    `max(floor, floor(0.95*live))` would set counter ≥ BuyItNowPrice and eBay
    would reject the call. Decline with explicit `unprofitable_floor_decline_*`
    reason instead of burning API quota on a guaranteed validation error."""
    cfg_bo = {"auto_accept_pct": 0.925, "auto_decline_pct": 0.75, "counter_offer_pct": 0.95}
    # live=£10, floor=£11
    #   auto_accept_gbp  = floor(0.925*10) = 9
    #   auto_decline_gbp = floor(0.75 *10) = 7
    #   counter_gbp      = max(11, floor(0.95*10)) = max(11, 9) = 11 ≥ live → unprofitable
    # buyer_offer=£8 sits in the in-band range (8 ≥ 7 and 8 < 9), normally would
    # trigger Counter; with unprofitable-floor guard it falls to decline.
    offer = {"buyer_offer_gbp": 8.0, "live_price_gbp": 10.0}
    decision = rbo.compute_decision(offer, cfg_bo, floor_gbp=11.0)
    assert decision["cron_action"] == "decline"
    assert decision["counter_price_gbp"] is None
    assert decision["reason"].startswith("unprofitable_floor_decline_")
    assert "floor_11" in decision["reason"]
    assert "live_10" in decision["reason"]


def test_compute_decision_normal_band_still_counters_when_floor_below_live() -> None:
    """Regression guard for the unprofitable-floor fix: normal listings where
    floor < live MUST still hit the counter branch."""
    cfg_bo = {"auto_accept_pct": 0.925, "auto_decline_pct": 0.75, "counter_offer_pct": 0.95}
    # live=£50, floor=£20 → counter_gbp = max(20, floor(0.95*50)) = 47 < 50
    offer = {"buyer_offer_gbp": 40.0, "live_price_gbp": 50.0}
    decision = rbo.compute_decision(offer, cfg_bo, floor_gbp=20.0)
    assert decision["cron_action"] == "counter"
    assert decision["counter_price_gbp"] == 47


def test_responder_auth_token_expired_detected_by_ebay_error_code(
    isolated_fees_config, isolated_ledger
) -> None:
    """Stage 5 R2 fix: auth-token expiry is detected by eBay numeric error
    code (932 / 16110 / 17470 / 21917) extracted from the exception .response,
    NOT by substring match on the message text. This test mocks ebaysdk
    raising a ConnectionError whose .response carries ErrorCode=932 and
    confirms the responder flips auth_expired and exits 1."""
    from ebaysdk.exception import ConnectionError as EbaySdkConnectionError

    pending = [_build_offer(buyer_offer_gbp=47.0)]

    class _StubResponse:
        def dict(self):  # noqa: D401
            return {"Errors": {"ErrorCode": "932", "ShortMessage": "internal token reference"}}

    auth_exc = EbaySdkConnectionError("internal token reference")
    auth_exc.response = _StubResponse()
    respond_mock = AsyncMock(side_effect=auth_exc)
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=pending)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", respond_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 1, "auth-token expiry must propagate exit 1"
    rows = _read_ledger(isolated_ledger)
    assert len(rows) == 1
    assert rows[0]["cron_action"] == "error"
    assert rows[0]["reason"] == "auth_expired"


def test_responder_substring_fallback_when_no_ebay_response_attached(
    isolated_fees_config, isolated_ledger
) -> None:
    """Stage 5 R2 regression guard: when an exception has NO .response
    (transport-layer failure before eBay responds), the substring fallback
    still detects auth-related messages. Without the fallback we'd silently
    misclassify token expiry as unexpected error."""
    pending = [_build_offer(buyer_offer_gbp=47.0)]
    transport_auth_exc = Exception("AuthToken has expired (transport-layer)")
    respond_mock = AsyncMock(side_effect=transport_auth_exc)
    with (
        patch.object(rbo, "get_pending_best_offers", AsyncMock(return_value=pending)),
        patch.object(rbo, "fetch_live_price_lookup", return_value={"287260458724": 50.0}),
        patch.object(rbo, "respond_to_best_offer", respond_mock),
    ):
        exit_code = rbo.main(["--apply", "--yes"])

    assert exit_code == 1
    rows = _read_ledger(isolated_ledger)
    assert rows[0]["reason"] == "auth_expired"
