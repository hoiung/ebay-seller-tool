"""Unit tests for ebay.client.execute_with_retry.

Guards two contracts:
1. New `files=` kwarg is forwarded to api.execute as a keyword argument.
2. Existing callers (no files=) keep the original two-positional-arg shape —
   any regression would be caught by the kwarg-absence assertion.
"""

from unittest.mock import MagicMock

import pytest

from ebay import client


def _fake_api(response_obj: object) -> MagicMock:
    """Return a MagicMock Trading API whose .execute() returns the given response."""
    api = MagicMock()
    api.execute.return_value = response_obj
    return api


def test_execute_with_retry_files_kwarg_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1.2 — files kwarg is forwarded explicitly via call_args.kwargs."""
    fake_response = MagicMock()
    api = _fake_api(fake_response)
    monkeypatch.setattr(client, "get_trading_api", lambda: api)

    payload_files = {"file": ("photo.jpg", b"binary-content", "image/jpeg")}
    result = client.execute_with_retry(
        "UploadSiteHostedPictures", {"WarningLevel": "High"}, files=payload_files
    )

    assert result is fake_response
    assert api.execute.call_count == 1
    # Explicit kwarg check — NOT **kwargs swallowing (AP #18).
    call_args = api.execute.call_args
    assert call_args.args == ("UploadSiteHostedPictures", {"WarningLevel": "High"})
    assert call_args.kwargs == {"files": payload_files}


@pytest.mark.parametrize(
    "verb,data",
    [
        ("ReviseFixedPriceItem", {"Item": {"ItemID": "123"}}),
        ("GetItem", {"ItemID": "123", "DetailLevel": "ReturnAll"}),
        ("GetMyeBaySelling", {"ActiveList": {"Sort": "TimeLeft"}}),
        ("GetTokenStatus", {}),
    ],
)
def test_execute_with_retry_existing_callers_unaffected(
    monkeypatch: pytest.MonkeyPatch, verb: str, data: dict
) -> None:
    """P1.3 — default call shape unchanged: no files kwarg reaches api.execute."""
    fake_response = MagicMock()
    api = _fake_api(fake_response)
    monkeypatch.setattr(client, "get_trading_api", lambda: api)

    result = client.execute_with_retry(verb, data)

    assert result is fake_response
    assert api.execute.call_count == 1
    call_args = api.execute.call_args
    assert call_args.args == (verb, data)
    # Critical: no kwargs at all — matches pre-patch two-arg shape byte-for-byte.
    assert call_args.kwargs == {}


def test_execute_with_retry_files_none_is_two_arg_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive — passing files=None explicitly must behave like omitting it."""
    fake_response = MagicMock()
    api = _fake_api(fake_response)
    monkeypatch.setattr(client, "get_trading_api", lambda: api)

    client.execute_with_retry("GetItem", {"ItemID": "1"}, files=None)

    call_args = api.execute.call_args
    assert call_args.args == ("GetItem", {"ItemID": "1"})
    assert call_args.kwargs == {}
