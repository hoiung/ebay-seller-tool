"""
eBay Trading API client layer.

Provides a singleton connection factory and retry-aware execution.
"""

import os
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache

from ebaysdk.trading import Connection as Trading


def log_debug(msg: str) -> None:
    """Log to stderr with timestamp. MCP uses stdout for protocol wire."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    print(f"[ebay-seller-tool {ts}] {msg}", file=sys.stderr, flush=True)


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
    if "EBAY_SITE_ID" in os.environ:
        site_id = os.environ["EBAY_SITE_ID"]
    else:
        # Fall back to config/fees.yaml so a single source drives marketplace.
        try:
            from ebay.fees import _load_fees_config  # noqa: PLC0415

            site_id = str(_load_fees_config()["ebay_uk"]["site_id"])
            log_debug(f"EBAY_SITE_ID unset, using config/fees.yaml site_id={site_id}")
        except (FileNotFoundError, KeyError, ValueError) as e:
            site_id = "3"
            log_debug(f"EBAY_SITE_ID unset, config fallback failed ({e}), defaulting to 3")

    sandbox = os.environ.get("EBAY_SANDBOX", "false").lower() == "true"
    if sandbox:
        domain = "api.sandbox.ebay.com"
        log_debug(f"Creating Trading API connection (site_id={site_id} SANDBOX={domain})")
    else:
        domain = "api.ebay.com"
        log_debug(f"Creating Trading API connection (site_id={site_id} domain={domain})")

    return Trading(
        appid=app_id,
        certid=cert_id,
        devid=dev_id,
        token=token,
        siteid=site_id,
        domain=domain,
        config_file=None,  # CRITICAL: suppress ebaysdk YAML config search
        timeout=10,  # Per-call HTTP timeout. Fits within MAX_CUMULATIVE_TIMEOUT_SECONDS budget.
        warnings=False,
    )


def reset_trading_api() -> None:
    """Clear the cached Trading API connection so the next call creates a fresh one.

    Useful for tests or scripts that need to switch environments (e.g. sandbox toggle).
    """
    get_trading_api.cache_clear()


# Retry budget — total wall-clock time the entire retry sequence may consume.
# Per Issue #1: "Max cumulative timeout 15s before giving up".
MAX_CUMULATIVE_TIMEOUT_SECONDS = 15


def execute_with_retry(
    verb: str,
    data: dict,
    max_attempts: int = 3,
    files: dict | None = None,
) -> object:
    """
    Execute a Trading API call with exponential backoff and a wall-clock budget.

    Retries on transient failures (HTTP 429 rate limit, network errors with no
    response attribute). Fails fast on application errors (eBay returns HTTP 200
    with errors in the XML body — ebaysdk raises its own ConnectionError).

    The total wall-clock time for the retry sequence is capped at
    MAX_CUMULATIVE_TIMEOUT_SECONDS. If the deadline is reached, the loop
    exits and the last error is raised.

    Args:
        verb: API verb (e.g. "GetMyeBaySelling", "GetTokenStatus")
        data: Request payload dict
        max_attempts: Maximum retry attempts (default 3)
        files: Optional multipart file dict for verbs like UploadSiteHostedPictures.
            Default None preserves the original two-arg execute() call shape so
            every existing caller behaviour is untouched.

    Returns:
        ebaysdk Response object with .reply attribute

    Raises:
        Exception: On API failure after retries exhausted or deadline reached
    """
    api = get_trading_api()
    backoff_seconds = [2, 4, 8]
    deadline = time.monotonic() + MAX_CUMULATIVE_TIMEOUT_SECONDS

    for attempt in range(max_attempts):
        if time.monotonic() >= deadline:
            log_debug(
                f"API {verb} DEADLINE_EXCEEDED attempt={attempt + 1}/{max_attempts} "
                f"budget={MAX_CUMULATIVE_TIMEOUT_SECONDS}s"
            )
            raise TimeoutError(
                f"API {verb} exceeded {MAX_CUMULATIVE_TIMEOUT_SECONDS}s cumulative budget"
            )

        log_debug(f"API {verb} CALLING attempt={attempt + 1}/{max_attempts}")
        start_ms = time.monotonic() * 1000
        try:
            response = (
                api.execute(verb, data, files=files) if files else api.execute(verb, data)
            )
            duration_ms = time.monotonic() * 1000 - start_ms
            log_debug(
                f"API {verb} OK duration_ms={duration_ms:.0f} attempt={attempt + 1}/{max_attempts}"
            )
            return response
        except Exception as e:
            duration_ms = time.monotonic() * 1000 - start_ms
            # ebaysdk raises its own ConnectionError, not builtins.ConnectionError.
            # status_code present = HTTP-level error; absent = network/transport error.
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            is_rate_limited = status_code == 429
            is_transport_error = status_code is None  # network drop, DNS, etc.
            is_retryable = is_rate_limited or is_transport_error

            if is_retryable and attempt < max_attempts - 1:
                delay = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                # Don't sleep past the deadline
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log_debug(
                        f"API {verb} DEADLINE_EXCEEDED no_retry "
                        f"attempt={attempt + 1}/{max_attempts}"
                    )
                    raise
                delay = min(delay, max(0, int(remaining)))
                reason = "RATE_LIMITED" if is_rate_limited else "TRANSPORT_ERROR"
                log_debug(
                    f"API {verb} {reason} duration_ms={duration_ms:.0f} "
                    f"attempt={attempt + 1}/{max_attempts} retry_in={delay}s "
                    f"error={type(e).__name__}: {e}"
                )
                time.sleep(delay)
                continue

            log_debug(
                f"API {verb} FAILED duration_ms={duration_ms:.0f} "
                f"attempt={attempt + 1}/{max_attempts} "
                f"status={status_code} error={type(e).__name__}: {e}"
            )
            raise

    # Unreachable — loop always returns or raises. Satisfies type checker.
    msg = f"API {verb} failed after {max_attempts} attempts"
    raise RuntimeError(msg)
