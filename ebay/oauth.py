"""
eBay OAuth session factories (Issue #4 Phase 2.2 + 3.1).

Two grant types, two separate sessions:

  user-token (authorization_code grant) → Analytics + Post-Order
    - One-time consent via scripts/oauth_setup.py
    - Refresh token stored in .env as EBAY_OAUTH_REFRESH_TOKEN
    - Access token cached in-memory; auto-refreshed before expiry

  app-token (client_credentials grant) → Browse (read-only public catalogue)
    - No user consent
    - EBAY_APP_CLIENT_ID + EBAY_APP_CLIENT_SECRET in .env
    - App token cached; auto-refreshed

Fail-fast contract (Issue #4 AC 2.1):
    - Missing refresh token at runtime → PermissionError with explicit instructions.
    - 401/403 on refresh → PermissionError with eBay's error body; no silent retry.
    - No fallback to Auth'N'Auth for user-token scopes.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from typing import Any

import httpx

from ebay.client import log_debug

# Endpoints (production). Override via EBAY_OAUTH_BASE_URL for sandbox.
_DEFAULT_BASE_URL = "https://api.ebay.com"


def _base_url() -> str:
    return os.environ.get("EBAY_OAUTH_BASE_URL", _DEFAULT_BASE_URL)


def _token_endpoint() -> str:
    return f"{_base_url()}/identity/v1/oauth2/token"


# Scopes for user-token: Analytics + Post-Order fulfillment read
USER_SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
)

# Scope for app-token: Browse
APP_SCOPE = "https://api.ebay.com/oauth/api_scope"


# --- In-memory token cache ---

_user_lock = threading.Lock()
_user_access_token: str | None = None
_user_expires_at: float = 0.0

_app_lock = threading.Lock()
_app_access_token: str | None = None
_app_expires_at: float = 0.0


def _client_id() -> str:
    cid = os.environ.get("EBAY_APP_CLIENT_ID")
    if not cid:
        raise PermissionError(
            "EBAY_APP_CLIENT_ID missing in .env — add production app client_id "
            "from https://developer.ebay.com/my/keys"
        )
    return cid


def _client_secret() -> str:
    cs = os.environ.get("EBAY_APP_CLIENT_SECRET")
    if not cs:
        raise PermissionError(
            "EBAY_APP_CLIENT_SECRET missing in .env — add production app cert_id "
            "from https://developer.ebay.com/my/keys"
        )
    return cs


def _basic_auth_header() -> str:
    raw = f"{_client_id()}:{_client_secret()}"
    return base64.b64encode(raw.encode()).decode()


def _refresh_user_token() -> tuple[str, float]:
    """Exchange refresh_token for a new access_token.

    Returns (access_token, expires_at_epoch). Raises PermissionError on 4xx.
    """
    refresh = os.environ.get("EBAY_OAUTH_REFRESH_TOKEN")
    if not refresh:
        raise PermissionError(
            "EBAY_OAUTH_REFRESH_TOKEN missing in .env — run "
            "scripts/oauth_setup.py and approve scopes: "
            f"{USER_SCOPES}"
        )

    headers = {
        "Authorization": f"Basic {_basic_auth_header()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": USER_SCOPES,
    }
    log_debug("OAUTH refreshing user-token")
    try:
        resp = httpx.post(_token_endpoint(), headers=headers, data=data, timeout=10.0)
    except httpx.HTTPError as e:
        raise PermissionError(f"OAuth refresh failed: network error {e}") from e

    if resp.status_code in (401, 403):
        raise PermissionError(
            f"OAuth refresh REJECTED ({resp.status_code}): {resp.text}. "
            "Refresh token may be revoked or expired — re-run scripts/oauth_setup.py."
        )
    if resp.status_code >= 400:
        raise PermissionError(f"OAuth refresh failed ({resp.status_code}): {resp.text}")

    payload = resp.json()
    access = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 7200))
    if not access:
        raise PermissionError(f"OAuth response missing access_token: {payload}")
    return access, time.time() + expires_in - 60  # 60s safety margin


def _refresh_app_token() -> tuple[str, float]:
    """Exchange client_id/secret for an app-token via client_credentials grant."""
    headers = {
        "Authorization": f"Basic {_basic_auth_header()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials", "scope": APP_SCOPE}
    log_debug("OAUTH refreshing app-token")
    try:
        resp = httpx.post(_token_endpoint(), headers=headers, data=data, timeout=10.0)
    except httpx.HTTPError as e:
        raise PermissionError(f"App-token refresh failed: network error {e}") from e

    if resp.status_code in (401, 403):
        raise PermissionError(
            f"App-token REJECTED ({resp.status_code}): {resp.text}. "
            "Check EBAY_APP_CLIENT_ID + EBAY_APP_CLIENT_SECRET in .env."
        )
    if resp.status_code >= 400:
        raise PermissionError(f"App-token refresh failed ({resp.status_code}): {resp.text}")

    payload = resp.json()
    access = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 7200))
    if not access:
        raise PermissionError(f"App-token response missing access_token: {payload}")
    return access, time.time() + expires_in - 60


def _get_user_access_token() -> str:
    global _user_access_token, _user_expires_at
    with _user_lock:
        if _user_access_token and time.time() < _user_expires_at:
            return _user_access_token
        _user_access_token, _user_expires_at = _refresh_user_token()
        return _user_access_token


def _get_app_access_token() -> str:
    global _app_access_token, _app_expires_at
    with _app_lock:
        if _app_access_token and time.time() < _app_expires_at:
            return _app_access_token
        _app_access_token, _app_expires_at = _refresh_app_token()
        return _app_access_token


def get_oauth_session() -> httpx.Client:
    """Return httpx.Client with a valid user-token Bearer header.

    Used by REST Analytics + Post-Order callers. Caller is responsible for
    closing the client (use as context manager).
    """
    token = _get_user_access_token()
    return httpx.Client(
        base_url=_base_url(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )


def get_post_order_session() -> httpx.Client:
    """Return httpx.Client with IAF (Auth'N'Auth) scheme — Post-Order API only.

    eBay's Post-Order v2 API rejects OAuth Bearer with 'Bad scheme: Bearer' error.
    It requires the legacy IAF scheme using the Auth'N'Auth token (same one used
    by Trading API calls). Verified live 2026-04-24 against /return/search.
    """
    auth_token = os.environ.get("EBAY_AUTH_TOKEN")
    if not auth_token:
        raise PermissionError("EBAY_AUTH_TOKEN missing — required for Post-Order API (IAF scheme)")
    return httpx.Client(
        base_url=_base_url(),
        headers={
            "Authorization": f"IAF {auth_token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_GB"),
        },
        timeout=15.0,
    )


def get_browse_session() -> httpx.Client:
    """Return httpx.Client with a valid app-token Bearer header (Browse API)."""
    token = _get_app_access_token()
    return httpx.Client(
        base_url=_base_url(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_GB"),
        },
        timeout=15.0,
    )


def reset_token_cache() -> None:
    """Clear cached tokens — tests call this."""
    global _user_access_token, _user_expires_at, _app_access_token, _app_expires_at
    with _user_lock:
        _user_access_token = None
        _user_expires_at = 0.0
    with _app_lock:
        _app_access_token = None
        _app_expires_at = 0.0


def on_401_refresh_and_retry(response: httpx.Response) -> None:
    """Raise PermissionError if we hit 401 on an authenticated call.

    Fail-fast per AC 2.7: 401 → loud PermissionError, no silent empty result.
    """
    if response.status_code == 401:
        raise PermissionError(
            f"eBay API returned 401 Unauthorized on {response.url}. "
            f"Body: {response.text}. Token may be revoked — re-run "
            "scripts/oauth_setup.py."
        )


_KNOWN_JSON_ERROR_KEYS = ("errors", "error", "errorMessage")


def raise_for_ebay_error(response: httpx.Response) -> None:
    """Fail-loud parser — raises if response.status >= 400 or eBay error envelope."""
    on_401_refresh_and_retry(response)
    if response.status_code >= 400:
        raise PermissionError(f"eBay API {response.status_code} on {response.url}: {response.text}")
    try:
        payload: Any = response.json()
    except ValueError:
        return
    if isinstance(payload, dict):
        for key in _KNOWN_JSON_ERROR_KEYS:
            if key in payload and payload[key]:
                raise PermissionError(f"eBay API error envelope on {response.url}: {payload[key]}")
