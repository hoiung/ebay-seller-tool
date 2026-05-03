"""Tests for ebay.end_listing — the safe-by-default EndFixedPriceItem wrapper.

The core function `end_listing(item_id, expected_title, ending_reason,
confirm, dry_run)` enforces:
  - Single-item only (no bulk path)
  - expected_title echo-back match (case-insensitive substring)
  - confirm=True required on live path
  - ending_reason from documented enum only
  - dry_run=True default — caller must opt in to destructive

These tests verify each guardrail rejects loudly + the happy path proceeds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ebay.end_listing import ALLOWED_ENDING_REASONS, end_listing


def _live_item(title: str = "Test Listing Title") -> SimpleNamespace:
    """Minimal SimpleNamespace mimicking an ebaysdk-parsed Item response."""
    return SimpleNamespace(
        ItemID="123",
        Title=title,
        SellingStatus=SimpleNamespace(
            CurrentPrice=SimpleNamespace(value="50.00", _currencyID="GBP"),
            QuantitySold="0",
        ),
        Quantity="1",
        ListingDetails=SimpleNamespace(
            ViewItemURL="https://www.ebay.co.uk/itm/123",
            StartTime="2026-04-01T10:00:00Z",
            EndTime="2026-05-01T10:00:00Z",
            RelistCount="0",
        ),
    )


def _getitem_response(item: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(reply=SimpleNamespace(Item=item))


def _end_response() -> SimpleNamespace:
    return SimpleNamespace(reply=SimpleNamespace(Ack="Success", EndTime="2026-04-30T12:00:00Z"))


def test_invalid_ending_reason_refused() -> None:
    """ending_reason outside the enum → ValueError, no API call."""
    with pytest.raises(ValueError, match="ending_reason must be one of"):
        asyncio.run(
            end_listing(
                item_id="123",
                expected_title="Test Listing Title",
                ending_reason="MadeUpReason",
            )
        )


def test_empty_expected_title_refused() -> None:
    with pytest.raises(ValueError, match="expected_title is required"):
        asyncio.run(end_listing(item_id="123", expected_title="", ending_reason="NotAvailable"))


def test_whitespace_only_expected_title_refused() -> None:
    with pytest.raises(ValueError, match="expected_title is required"):
        asyncio.run(end_listing(item_id="123", expected_title="   "))


def test_live_path_without_confirm_refused() -> None:
    """dry_run=False without confirm=True → refuse."""
    with pytest.raises(ValueError, match="requires confirm=True"):
        asyncio.run(
            end_listing(
                item_id="123",
                expected_title="Test",
                dry_run=False,
                confirm=False,
            )
        )


def test_item_not_found_raises() -> None:
    """If GetItem returns no Item, raise ValueError."""
    with patch("ebay.end_listing.execute_with_retry") as mock_exec:
        mock_exec.return_value = _getitem_response(None)
        with pytest.raises(ValueError, match="not found or no longer active"):
            asyncio.run(end_listing(item_id="999", expected_title="anything"))


def test_title_mismatch_refused_before_side_effect() -> None:
    """Echo-back guard fires BEFORE any EndFixedPriceItem call."""
    with patch("ebay.end_listing.execute_with_retry") as mock_exec:
        mock_exec.return_value = _getitem_response(_live_item("HPE 3TB Hard Drive"))
        with pytest.raises(ValueError, match="expected_title mismatch"):
            asyncio.run(
                end_listing(
                    item_id="123",
                    expected_title="Seagate SSD",
                    confirm=True,
                    dry_run=False,
                )
            )
        # Only GetItem was called; EndFixedPriceItem must NOT fire on mismatch.
        verbs_called = [c.args[0] for c in mock_exec.call_args_list]
        assert verbs_called == ["GetItem"]


def test_dry_run_returns_preview_no_side_effect() -> None:
    """dry_run=True does NOT call EndFixedPriceItem."""
    with patch("ebay.end_listing.execute_with_retry") as mock_exec:
        mock_exec.return_value = _getitem_response(_live_item("HPE 3TB Hard Drive"))
        result = asyncio.run(
            end_listing(
                item_id="123",
                expected_title="HPE 3TB",
                dry_run=True,
            )
        )
    assert result["dry_run"] is True
    assert result["would_end"] is True
    assert result["item_id"] == "123"
    assert result["ending_reason"] == "NotAvailable"
    assert result["live_title_pre"] == "HPE 3TB Hard Drive"
    # Only GetItem; EndFixedPriceItem refused by dry-run path.
    verbs_called = [c.args[0] for c in mock_exec.call_args_list]
    assert "EndFixedPriceItem" not in verbs_called


def test_case_insensitive_title_match() -> None:
    """expected_title matches case-insensitively against live title."""
    with patch("ebay.end_listing.execute_with_retry") as mock_exec:
        mock_exec.return_value = _getitem_response(_live_item("HPE 3TB Hard Drive"))
        # Caller passes lowercase substring — must still match
        result = asyncio.run(
            end_listing(item_id="123", expected_title="hpe 3tb", dry_run=True)
        )
    assert result["dry_run"] is True


def test_live_path_calls_end_fixed_price_item() -> None:
    """Live path with valid title + confirm + dry_run=False fires EndFixedPriceItem."""
    with patch("ebay.end_listing.execute_with_retry") as mock_exec, patch(
        "ebay.end_listing.audit_log_write"
    ) as mock_audit:
        # First call = GetItem, second call = EndFixedPriceItem
        mock_exec.side_effect = [
            _getitem_response(_live_item("HPE 3TB Hard Drive")),
            _end_response(),
        ]
        result = asyncio.run(
            end_listing(
                item_id="123",
                expected_title="HPE 3TB",
                ending_reason="NotAvailable",
                confirm=True,
                dry_run=False,
            )
        )
    assert result["ok"] is True
    assert result["ack"] == "Success"
    assert result["end_time"] == "2026-04-30T12:00:00Z"
    assert result["ending_reason"] == "NotAvailable"
    verbs_called = [c.args[0] for c in mock_exec.call_args_list]
    assert verbs_called == ["GetItem", "EndFixedPriceItem"]
    # Audit log called exactly once with success=True
    assert mock_audit.call_count == 1
    kwargs = mock_audit.call_args.kwargs
    assert kwargs["success"] is True
    assert kwargs["fields_changed"] == ["END"]
    assert kwargs["condition_after"] == "NotAvailable"


def test_live_path_audit_logs_failure_on_api_error() -> None:
    """If EndFixedPriceItem raises, audit_log_write records success=False and re-raises."""

    class FakeEbayError(Exception):
        pass

    with patch("ebay.end_listing.execute_with_retry") as mock_exec, patch(
        "ebay.end_listing.audit_log_write"
    ) as mock_audit:
        mock_exec.side_effect = [
            _getitem_response(_live_item("HPE 3TB Hard Drive")),
            FakeEbayError("eBay rejected: rate limit"),
        ]
        with pytest.raises(FakeEbayError):
            asyncio.run(
                end_listing(
                    item_id="123",
                    expected_title="HPE 3TB",
                    confirm=True,
                    dry_run=False,
                )
            )
    assert mock_audit.call_count == 1
    assert mock_audit.call_args.kwargs["success"] is False


def test_m10_already_ended_translated_to_friendly_valueerror() -> None:
    """M10 (Ralph deferred Opus) -- ebaysdk ConnectionError carrying eBay code 1037
    ("Listing already ended") gets translated into a ValueError telling the
    operator to re-fetch state. audit_log_write still fires success=False.
    """
    from ebaysdk.exception import ConnectionError as EbaySdkConnectionError

    fake_response = SimpleNamespace()
    fake_response.dict = lambda: {  # type: ignore[attr-defined]
        "Errors": {"ErrorCode": "1037", "LongMessage": "Listing already ended"}
    }
    err = EbaySdkConnectionError("eBay 1037")
    err.response = fake_response  # type: ignore[attr-defined]

    with patch("ebay.end_listing.execute_with_retry") as mock_exec, patch(
        "ebay.end_listing.audit_log_write"
    ) as mock_audit:
        mock_exec.side_effect = [
            _getitem_response(_live_item("HPE 3TB Hard Drive")),
            err,
        ]
        with pytest.raises(ValueError, match=r"changed between GetItem and EndFixedPriceItem"):
            asyncio.run(
                end_listing(
                    item_id="123",
                    expected_title="HPE 3TB",
                    confirm=True,
                    dry_run=False,
                )
            )
    assert mock_audit.call_count == 1
    assert mock_audit.call_args.kwargs["success"] is False


def test_m10_unrelated_connectionerror_reraised_unchanged() -> None:
    """M10 (Ralph deferred Opus) -- ConnectionError WITHOUT a known
    operation-not-allowed code re-raises the original exception (genuine
    transport / API failure path).
    """
    from ebaysdk.exception import ConnectionError as EbaySdkConnectionError

    fake_response = SimpleNamespace()
    fake_response.dict = lambda: {  # type: ignore[attr-defined]
        "Errors": {"ErrorCode": "10009", "LongMessage": "Internal error"}
    }
    err = EbaySdkConnectionError("eBay 500")
    err.response = fake_response  # type: ignore[attr-defined]

    with patch("ebay.end_listing.execute_with_retry") as mock_exec, patch(
        "ebay.end_listing.audit_log_write"
    ) as mock_audit:
        mock_exec.side_effect = [
            _getitem_response(_live_item("HPE 3TB Hard Drive")),
            err,
        ]
        with pytest.raises(EbaySdkConnectionError):
            asyncio.run(
                end_listing(
                    item_id="123",
                    expected_title="HPE 3TB",
                    confirm=True,
                    dry_run=False,
                )
            )
    assert mock_audit.call_count == 1
    assert mock_audit.call_args.kwargs["success"] is False


def test_m10_extract_ebay_error_codes_handles_list_of_errors() -> None:
    """M10 (Ralph deferred Opus) -- helper handles both single-Error and
    multi-Error response shapes without crashing.
    """
    from ebaysdk.exception import ConnectionError as EbaySdkConnectionError

    from ebay.end_listing import _extract_ebay_error_codes

    # Single Error dict
    r1 = SimpleNamespace()
    r1.dict = lambda: {"Errors": {"ErrorCode": "1037"}}  # type: ignore[attr-defined]
    e1 = EbaySdkConnectionError("x")
    e1.response = r1  # type: ignore[attr-defined]
    assert _extract_ebay_error_codes(e1) == {"1037"}

    # Multi-Error list
    r2 = SimpleNamespace()
    r2.dict = lambda: {  # type: ignore[attr-defined]
        "Errors": [{"ErrorCode": "1037"}, {"ErrorCode": "1047"}]
    }
    e2 = EbaySdkConnectionError("x")
    e2.response = r2  # type: ignore[attr-defined]
    assert _extract_ebay_error_codes(e2) == {"1037", "1047"}

    # No response attribute
    e3 = EbaySdkConnectionError("no response")
    assert _extract_ebay_error_codes(e3) == set()

    # Response without .dict()
    r4 = SimpleNamespace()
    e4 = EbaySdkConnectionError("malformed")
    e4.response = r4  # type: ignore[attr-defined]
    assert _extract_ebay_error_codes(e4) == set()

    # Response with no Errors key
    r5 = SimpleNamespace()
    r5.dict = lambda: {"Ack": "Success"}  # type: ignore[attr-defined]
    e5 = EbaySdkConnectionError("ok-shape")
    e5.response = r5  # type: ignore[attr-defined]
    assert _extract_ebay_error_codes(e5) == set()


def test_allowed_ending_reasons_enum_contents() -> None:
    """Enum frozen at expected eBay values per Trading API docs."""
    assert "NotAvailable" in ALLOWED_ENDING_REASONS
    assert "LostOrBroken" in ALLOWED_ENDING_REASONS
    assert "Incorrect" in ALLOWED_ENDING_REASONS
    assert "OtherListingError" in ALLOWED_ENDING_REASONS


def test_each_allowed_ending_reason_accepted() -> None:
    """Every documented reason should pass enum validation in dry-run."""
    with patch("ebay.end_listing.execute_with_retry") as mock_exec:
        mock_exec.return_value = _getitem_response(_live_item("HPE 3TB Hard Drive"))
        for reason in ALLOWED_ENDING_REASONS:
            result = asyncio.run(
                end_listing(
                    item_id="123",
                    expected_title="HPE 3TB",
                    ending_reason=reason,
                    dry_run=True,
                )
            )
            assert result["ending_reason"] == reason


def test_default_ending_reason_is_not_available() -> None:
    with patch("ebay.end_listing.execute_with_retry") as mock_exec:
        mock_exec.return_value = _getitem_response(_live_item("HPE 3TB Hard Drive"))
        result = asyncio.run(
            end_listing(item_id="123", expected_title="HPE 3TB", dry_run=True)
        )
    assert result["ending_reason"] == "NotAvailable"


def test_mcp_tool_wrapper_returns_json_envelope() -> None:
    """server.end_listing wraps ValueError → {"error": ...} JSON envelope."""
    import json

    import server

    with patch("server._end_listing_core") as mock_core:
        mock_core.side_effect = ValueError("test failure")
        raw = asyncio.run(
            server.end_listing(
                item_id="123",
                expected_title="HPE 3TB",
                dry_run=True,
            )
        )
    body = json.loads(raw)
    assert body["error"] == "test failure"
    # Surfaces the allowed reasons list so callers can self-correct
    assert "NotAvailable" in body["allowed_ending_reasons"]
