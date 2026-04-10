"""
eBay Trading API client layer.

Provides a singleton connection factory and retry-aware execution.
"""

import os
import sys
import time
from functools import lru_cache

from ebaysdk.trading import Connection as Trading


def log_debug(msg: str) -> None:
    """Log to stderr (MCP uses stdout for protocol wire)."""
    print(f"[ebay-seller-tool] {msg}", file=sys.stderr, flush=True)


@lru_cache(maxsize=1)
def get_trading_api() -> Trading:
    """
    Singleton factory for eBay Trading API connection.

    Safe to reuse: Connection._reset() is called on every execute(),
    so the singleton doesn't carry state between API calls.
    """
    app_id = os.environ["EBAY_APP_ID"]
    cert_id = os.environ["EBAY_CERT_ID"]
    dev_id = os.environ["EBAY_DEV_ID"]
    token = os.environ["EBAY_AUTH_TOKEN"]
    site_id = os.environ.get("EBAY_SITE_ID", "3")

    log_debug(f"Creating Trading API connection (site_id={site_id})")

    return Trading(
        appid=app_id,
        certid=cert_id,
        devid=dev_id,
        token=token,
        siteid=site_id,
        config_file=None,  # CRITICAL: suppress ebaysdk YAML config search
        timeout=30,
        warnings=False,
    )


def execute_with_retry(
    verb: str,
    data: dict,
    max_attempts: int = 3,
) -> object:
    """
    Execute a Trading API call with exponential backoff on rate limits.

    Retries on HTTP 429 only. Fails fast on application errors (eBay always
    returns HTTP 200 for app errors — the SDK raises ConnectionError).

    Args:
        verb: API verb (e.g. "GetMyeBaySelling", "GetTokenStatus")
        data: Request payload dict
        max_attempts: Maximum retry attempts (default 3)

    Returns:
        ebaysdk response object (dict-like)

    Raises:
        ConnectionError: On API failure after retries exhausted
    """
    api = get_trading_api()
    backoff_seconds = [2, 4, 8]

    for attempt in range(max_attempts):
        start_ms = time.monotonic() * 1000
        try:
            response = api.execute(verb, data)
            duration_ms = time.monotonic() * 1000 - start_ms
            log_debug(
                f"API {verb} OK duration_ms={duration_ms:.0f} "
                f"attempt={attempt + 1}/{max_attempts}"
            )
            return response
        except ConnectionError as e:
            duration_ms = time.monotonic() * 1000 - start_ms
            status_code = getattr(getattr(e, "response", None), "status_code", None)

            if status_code == 429 and attempt < max_attempts - 1:
                delay = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                log_debug(
                    f"API {verb} RATE_LIMITED duration_ms={duration_ms:.0f} "
                    f"attempt={attempt + 1}/{max_attempts} retry_in={delay}s"
                )
                time.sleep(delay)
                continue

            log_debug(
                f"API {verb} FAILED duration_ms={duration_ms:.0f} "
                f"attempt={attempt + 1}/{max_attempts} "
                f"status={status_code} error={e}"
            )
            raise

    # Should not reach here, but satisfy type checker
    msg = f"API {verb} failed after {max_attempts} attempts"
    raise ConnectionError(msg)
