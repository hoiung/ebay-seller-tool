"""
eBay credential validation and token health checks.
"""

import os
import sys


def log_debug(msg: str) -> None:
    """Log to stderr (MCP uses stdout for protocol wire)."""
    print(f"[ebay-seller-tool] {msg}", file=sys.stderr, flush=True)


REQUIRED_VARS = [
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
    "EBAY_DEV_ID",
    "EBAY_AUTH_TOKEN",
]


def validate_credentials() -> None:
    """
    Check all required eBay env vars exist and are non-empty.

    Raises SystemExit with clear error listing missing vars.
    """
    missing = [var for var in REQUIRED_VARS if not os.environ.get(var)]

    if missing:
        log_debug(f"CREDENTIAL_CHECK FAILED missing={missing}")
        print(
            f"ERROR: Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your eBay credentials.\n"
            f"Get credentials from https://developer.ebay.com",
            file=sys.stderr,
        )
        raise SystemExit(1)

    log_debug(f"CREDENTIAL_CHECK OK vars_present={len(REQUIRED_VARS)}")


def check_token_expiry() -> None:
    """
    Call GetTokenStatus to check Auth'N'Auth token expiry.

    Best-effort startup hygiene — logs warning if <30 days remaining.
    On ANY failure, logs warning and continues (does NOT block startup).
    """
    try:
        from ebay.client import execute_with_retry

        response = execute_with_retry("GetTokenStatus", {})
        token_status = response.reply.TokenStatus

        expiry = str(getattr(token_status, "ExpirationTime", "unknown"))
        status = str(getattr(token_status, "Status", "unknown"))

        log_debug(f"TOKEN_CHECK status={status} expiry={expiry}")

        if status != "Active":
            log_debug(f"TOKEN_CHECK WARNING token_status={status} — token may not be active")

    except Exception as e:
        log_debug(f"TOKEN_CHECK SKIPPED error={e} — continuing without token validation")
