"""
eBay credential validation and token health checks.
"""

import os

from ebay.client import log_debug

REQUIRED_VARS = [
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
    "EBAY_DEV_ID",
    "EBAY_AUTH_TOKEN",
    "EBAY_SELLER_LOCATION",
    "EBAY_SELLER_POSTCODE",
]


def validate_credentials() -> None:
    """
    Check all required eBay env vars exist and are non-empty.

    Raises SystemExit with clear error listing missing vars.
    """
    missing = [var for var in REQUIRED_VARS if not os.environ.get(var)]

    if missing:
        log_debug(f"CREDENTIAL_CHECK FAILED missing={missing}")
        log_debug(
            f"ERROR: Missing required environment variables: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill in your eBay credentials. "
            f"Get credentials from https://developer.ebay.com"
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
        from datetime import datetime, timezone

        from ebay.client import execute_with_retry

        response = execute_with_retry("GetTokenStatus", {})
        token_status = response.reply.TokenStatus

        expiry_str = str(getattr(token_status, "ExpirationTime", "unknown"))
        status = str(getattr(token_status, "Status", "unknown"))

        log_debug(f"TOKEN_CHECK status={status} expiry={expiry_str}")

        if status != "Active":
            log_debug(f"TOKEN_CHECK WARNING token_status={status} — token may not be active")

        # Parse expiry and warn if <30 days remaining
        try:
            expiry_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            days_remaining = (expiry_dt - datetime.now(timezone.utc)).days
            log_debug(f"TOKEN_CHECK days_remaining={days_remaining}")
            if days_remaining < 30:
                log_debug(
                    f"TOKEN_CHECK WARNING token expires in {days_remaining} days — "
                    f"renew at eBay Developer portal"
                )
        except (ValueError, TypeError):
            pass  # Expiry string not parseable — already logged raw value above

    except Exception as e:
        log_debug(f"TOKEN_CHECK SKIPPED error={e} — continuing without token validation")
