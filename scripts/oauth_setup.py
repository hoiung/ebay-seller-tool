"""
One-time OAuth user-token setup for Analytics + Post-Order (Issue #4 AC 2.1).

Uses the authorization code grant. eBay seller-owned data on Analytics +
Post-Order requires a user-token; neither API supports a simpler app-token
path (verified: https://developer.ebay.com/api-docs/static/oauth-authorization-code-grant.html).

Usage:
    1. Create a production app in https://developer.ebay.com/my/keys
    2. Note the client_id + client_secret + RuName (redirect URL)
    3. Add to .env:
        EBAY_APP_CLIENT_ID=...
        EBAY_APP_CLIENT_SECRET=...
        EBAY_OAUTH_RU_NAME=...
    4. Run this script:
        uv run python scripts/oauth_setup.py
    5. Browser opens. Approve scopes.
    6. After redirect, the script writes EBAY_OAUTH_REFRESH_TOKEN back to .env.

Fail-fast:
    - consent denied / callback closed → PermissionError with consent re-run instructions.
    - exchange failure → raises with eBay's error body.
    - No silent fallback.
"""

from __future__ import annotations

import base64
import os
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

_BASE = "https://auth.ebay.com/oauth2/authorize"
_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SCOPES = (
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
)


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}"
    return base64.b64encode(raw.encode()).decode()


def _build_consent_url(client_id: str, ru_name: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": ru_name,
        "scope": _SCOPES,
        "prompt": "login",
    }
    return f"{_BASE}?{urllib.parse.urlencode(params)}"


def _exchange_code_for_refresh_token(
    code: str, client_id: str, client_secret: str, ru_name: str
) -> dict[str, str]:
    headers = {
        "Authorization": f"Basic {_basic_auth(client_id, client_secret)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ru_name,
    }
    resp = httpx.post(_TOKEN_URL, headers=headers, data=data, timeout=10.0)
    if resp.status_code >= 400:
        raise PermissionError(
            f"Token exchange failed ({resp.status_code}): {resp.text}. "
            "Verify client_id / client_secret / RuName and re-run."
        )
    payload = resp.json()
    if "refresh_token" not in payload:
        raise PermissionError(f"No refresh_token in response: {payload}")
    return payload


def _append_to_env(path: Path, key: str, value: str) -> None:
    """Set or replace KEY=VALUE in .env. Preserves other lines."""
    if not path.exists():
        path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    client_id = os.environ.get("EBAY_APP_CLIENT_ID")
    client_secret = os.environ.get("EBAY_APP_CLIENT_SECRET")
    ru_name = os.environ.get("EBAY_OAUTH_RU_NAME")
    missing = [
        k
        for k in ("EBAY_APP_CLIENT_ID", "EBAY_APP_CLIENT_SECRET", "EBAY_OAUTH_RU_NAME")
        if not os.environ.get(k)
    ]
    if missing:
        print(f"ERROR: missing env vars: {missing}", file=sys.stderr)
        print(
            "Add production-app credentials from https://developer.ebay.com/my/keys "
            "to .env then retry.",
            file=sys.stderr,
        )
        return 1
    assert client_id and client_secret and ru_name  # satisfy type checker

    consent_url = _build_consent_url(client_id, ru_name)
    print(f"Opening consent URL in browser: {consent_url}")
    webbrowser.open(consent_url)

    print("\nAfter approving, eBay redirects you to your RuName URL.")
    print("Paste the FULL redirect URL here (it contains ?code=...):")
    redirect_url = input("redirect URL: ").strip()
    if not redirect_url:
        raise PermissionError(
            "OAuth consent required. No redirect URL provided. "
            f"Re-run scripts/oauth_setup.py and approve scopes: {_SCOPES}"
        )
    parsed = urlparse(redirect_url)
    code = parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        raise PermissionError(
            "No authorization code in redirect URL. "
            "User may have denied consent — re-run and approve."
        )

    payload = _exchange_code_for_refresh_token(code, client_id, client_secret, ru_name)
    refresh_token = payload["refresh_token"]
    env_path = Path(__file__).parent.parent / ".env"
    _append_to_env(env_path, "EBAY_OAUTH_REFRESH_TOKEN", refresh_token)
    print(f"\nRefresh token written to {env_path}")
    expires = payload.get("refresh_token_expires_in", "unknown")
    print(f"Refresh token expires in ~{expires} seconds (eBay issues ~18-month tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
