"""Unit tests for ebay.oauth — fail-fast contract (Issue #4 AC 2.1, 2.7)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from ebay import oauth


def setup_function() -> None:
    oauth.reset_token_cache()


def test_missing_refresh_token_raises_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EBAY_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("EBAY_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("EBAY_APP_CLIENT_SECRET", "secret")
    with pytest.raises(PermissionError, match="EBAY_OAUTH_REFRESH_TOKEN missing"):
        oauth._refresh_user_token()


def test_missing_client_id_raises_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EBAY_APP_CLIENT_ID", raising=False)
    with pytest.raises(PermissionError, match="EBAY_APP_CLIENT_ID missing"):
        oauth._client_id()


def test_refresh_user_token_401_raises_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBAY_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("EBAY_APP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_OAUTH_REFRESH_TOKEN", "stale-refresh")
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 401
    fake_resp.text = '{"error":"invalid_grant"}'
    with patch("ebay.oauth.httpx.post", return_value=fake_resp):
        with pytest.raises(PermissionError, match="REJECTED"):
            oauth._refresh_user_token()


def test_refresh_user_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBAY_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("EBAY_APP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_OAUTH_REFRESH_TOKEN", "good-refresh")
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"access_token": "ACCESS-XYZ", "expires_in": 7200}
    with patch("ebay.oauth.httpx.post", return_value=fake_resp):
        token, expires = oauth._refresh_user_token()
    assert token == "ACCESS-XYZ"
    assert expires > 0


def test_refresh_app_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBAY_APP_CLIENT_ID", "cid")
    monkeypatch.setenv("EBAY_APP_CLIENT_SECRET", "secret")
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"access_token": "APP-ABC", "expires_in": 7200}
    with patch("ebay.oauth.httpx.post", return_value=fake_resp):
        token, expires = oauth._refresh_app_token()
    assert token == "APP-ABC"


def test_on_401_raises() -> None:
    fake = MagicMock(spec=httpx.Response)
    fake.status_code = 401
    fake.url = "https://api.ebay.com/test"
    fake.text = "unauthorized"
    with pytest.raises(PermissionError, match="401"):
        oauth.on_401_refresh_and_retry(fake)


def test_raise_for_ebay_error_skips_ok() -> None:
    fake = MagicMock(spec=httpx.Response)
    fake.status_code = 200
    fake.url = "https://api.ebay.com/test"
    fake.json.return_value = {"result": "ok"}
    # Should not raise
    oauth.raise_for_ebay_error(fake)


def test_raise_for_ebay_error_detects_envelope() -> None:
    fake = MagicMock(spec=httpx.Response)
    fake.status_code = 200
    fake.url = "https://api.ebay.com/test"
    fake.json.return_value = {"errors": [{"message": "boom"}]}
    with pytest.raises(PermissionError, match="error envelope"):
        oauth.raise_for_ebay_error(fake)
