"""
End-listing helper (Issue ebay-ops#10 — gap surfaced during AP #18 sample design).

`end_listing(item_id, expected_title, ending_reason, confirm=False, dry_run=True)`
is the first-class MCP-tool wrapper around Trading API `EndFixedPriceItem`.

Designed to be safe-by-default. `EndFixedPriceItem` is destructive: once a
listing is ended, the original ItemID is gone (eBay supports relisting from
history, but with a NEW ItemID). Multiple guardrails reduce the chance of a
fat-fingered close ending the wrong listing or every listing in a loop:

  1. Single-item only — no bulk path in this module. A bulk wrapper, if ever
     needed, would have to live elsewhere and walk this function per-item.
  2. `expected_title` echo-back match — the caller MUST quote the listing's
     current live title (case-insensitive substring match). Mismatch → refuse
     loudly. This catches "wrong ItemID" mistakes before any side-effect.
  3. `confirm=True` required — guards the destructive path.
  4. Explicit `ending_reason` enum from the eBay-allowed set; no free text.
  5. `dry_run=True` is the default — caller has to explicitly opt in to the
     destructive path. Mirrors `create_listing`'s default-dry-run posture.
  6. Audit log entry per call (success or failure) via `audit_log_write`.
"""

from __future__ import annotations

import asyncio

from ebaysdk.exception import ConnectionError as EbaySdkConnectionError

from ebay.client import execute_with_retry, log_debug
from ebay.listings import audit_log_write, listing_to_dict

# eBay error codes that indicate the listing changed between GetItem (step 1)
# and EndFixedPriceItem (step 4) — i.e. the work is already done OR the listing
# transitioned to a state where ending is no longer applicable. These are
# operationally distinct from a genuine network/API failure: we friendly-message
# the operator, write an audit-log entry, then re-raise so the caller can decide.
# Source: https://developer.ebay.com/devzone/xml/docs/reference/ebay/Errors/index.html
_LISTING_ALREADY_ENDED_CODES = frozenset(
    {
        "1037",  # Listing has already been ended
        "1047",  # End reason invalid for the current state
        "16306",  # Operation not allowed for this listing state
        "291",  # Operation not allowed at this time
    }
)

# Issue #32 Phase 2 — canonical eBay Trading API auth-token error codes.
# Single source of truth, imported by best_offers.py + respond_best_offers.py
# (cross-repo). Replaces three drift-prone copies with one. Stable contract per
# https://developer.ebay.com/devzone/xml/docs/Reference/eBay/Errors/ErrorMessages.htm
_AUTH_ERROR_CODES = frozenset(
    {
        "932",    # Auth token is invalid
        "16110",  # Auth token expired (soft)
        "17470",  # Auth token expired
        "21917",  # Token validation failed
    }
)

# Issue #32 Phase 2 — eBay error codes that indicate an offer is not actionable
# by the seller. Membership trimmed to evidence-grounded codes only (JBGE):
#   21940 — "Cannot respond to your own counter offer" (live evidence: cron loop
#           on m.k_1978 SellerCounterOffer item 264666106, 2026-05-04..05).
# Future expansion: add new code only when live evidence shows the responder
# hits it. The Phase 1 BuyerBestOffer allowlist drops most non-respondable
# states at parse time; this set covers the residual API-side failures.
_NON_RESPONDABLE_CODES = frozenset(
    {
        "21940",  # Cannot respond to your own counter offer
    }
)

# eBay's documented EndFixedPriceItem.EndingReason enum (Trading API reference).
# Source: https://developer.ebay.com/devzone/xml/docs/Reference/eBay/EndFixedPriceItem.html
ALLOWED_ENDING_REASONS: tuple[str, ...] = (
    "NotAvailable",
    "LostOrBroken",
    "Incorrect",
    "OtherListingError",
    "SellToHighBidder",  # auction-only on eBay's side, but accepted on Revise too
)


async def end_listing(
    item_id: str,
    expected_title: str,
    ending_reason: str = "NotAvailable",
    confirm: bool = False,
    dry_run: bool = True,
) -> dict:
    """End a single live listing via Trading API EndFixedPriceItem.

    Args:
        item_id: eBay item ID.
        expected_title: caller's claimed title for this listing — checked
            case-insensitively as a substring match against the live title.
            Mismatch → refuse before any side-effect. Callers SHOULD pass the
            full live title (round-trip via get_listing_details first).
        ending_reason: one of `ALLOWED_ENDING_REASONS`. Defaults to 'NotAvailable'
            (the most common case — out-of-stock).
        confirm: REQUIRED True to actually end. Defaults False as belt-and-braces
            against accidental destructive calls.
        dry_run: True (default) returns a preview without calling EndFixedPriceItem.
            Set False to actually end.

    Returns:
        dict with keys: ok, item_id, ending_reason, dry_run, expected_title,
        live_title_pre, end_time (live only), ack (live only).

    Raises:
        ValueError: invalid ending_reason, expected_title mismatch, item not
            found, confirm not True on live path.
    """
    if ending_reason not in ALLOWED_ENDING_REASONS:
        raise ValueError(
            f"ending_reason must be one of {ALLOWED_ENDING_REASONS!r}, got {ending_reason!r}"
        )
    if not expected_title or not expected_title.strip():
        raise ValueError("expected_title is required (echo-back guard)")
    if not dry_run and not confirm:
        raise ValueError(
            "live end_listing requires confirm=True (destructive — listing "
            "cannot be un-ended; relisting requires a new ItemID)"
        )

    log_debug(
        f"end_listing item_id={item_id} reason={ending_reason} dry_run={dry_run} confirm={confirm}"
    )

    # 1. Fetch live listing — extract title for echo-back check.
    current = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll"},
    )
    if current.reply.Item is None:
        raise ValueError(f"item {item_id} not found or no longer active")

    listing = listing_to_dict(current.reply.Item)
    live_title = listing.get("title") or ""
    live_url = listing["listing_url"]

    # 2. Echo-back guard — case-insensitive substring match.
    if expected_title.strip().lower() not in live_title.lower():
        raise ValueError(
            f"expected_title mismatch: caller passed {expected_title!r} but "
            f"live title is {live_title!r}. Refusing to end — caller may have "
            f"the wrong item_id."
        )

    # 3. Dry-run: project the action without calling EndFixedPriceItem.
    if dry_run:
        return {
            "dry_run": True,
            "item_id": item_id,
            "ending_reason": ending_reason,
            "expected_title": expected_title,
            "live_title_pre": live_title,
            "would_end": True,
            "live_url": live_url,
        }

    # 4. Live path — call EndFixedPriceItem.
    try:
        response = await asyncio.to_thread(
            execute_with_retry,
            "EndFixedPriceItem",
            {"ItemID": item_id, "EndingReason": ending_reason},
        )
    except EbaySdkConnectionError as exc:  # noqa: BLE001 — narrow class below
        # Distinguish "listing already changed" (caller should re-fetch) from
        # genuine API failure. M10 fix: prior bare `except Exception` hid this
        # signal under the generic re-raise.
        error_codes = _extract_ebay_error_codes(exc)
        already_ended = bool(_LISTING_ALREADY_ENDED_CODES & error_codes)
        audit_log_write(
            item_id=item_id,
            fields_changed=["END"],
            before_length=len(live_title),
            after_length=0,
            success=False,
            condition_after=ending_reason,
        )
        if already_ended:
            log_debug(
                f"end_listing item_id={item_id} already-ended-or-frozen; "
                f"codes={sorted(error_codes)} — re-fetch listing state"
            )
            raise ValueError(
                f"listing {item_id} changed between GetItem and EndFixedPriceItem "
                f"(eBay codes {sorted(error_codes)}). Re-fetch state via "
                f"get_listing_details and decide if action is still required."
            ) from exc
        raise
    except Exception:
        # Defensive — non-eBaysdk failures (e.g. asyncio cancellation, threading
        # errors). Audit-log + re-raise so the caller still sees the failure.
        audit_log_write(
            item_id=item_id,
            fields_changed=["END"],
            before_length=len(live_title),
            after_length=0,
            success=False,
            condition_after=ending_reason,
        )
        raise

    ack = str(getattr(response.reply, "Ack", ""))
    end_time = str(getattr(response.reply, "EndTime", ""))

    audit_log_write(
        item_id=item_id,
        fields_changed=["END"],
        before_length=len(live_title),
        after_length=0,
        success=True,
        condition_after=ending_reason,
    )

    return {
        "ok": True,
        "item_id": item_id,
        "ending_reason": ending_reason,
        "expected_title": expected_title,
        "live_title_pre": live_title,
        "end_time": end_time,
        "ack": ack,
        "dry_run": False,
        "live_url": live_url,
    }


def _classify_ebay_error_codes(codes: set[str], message: str = "") -> str:
    """Classify an eBay API error into one of {auth, non_respondable, transport, unknown}.

    Issue #32 Phase 4 — single classifier for all error-bucketing needs across
    `best_offers.py` (per-item sweep + Phase 3 stats counters) and
    `respond_best_offers.py` (per-offer error reason).

    Precedence (auth > non_respondable > transport > unknown) — when a
    response carries multiple codes (e.g. `21917` AND `21940`), the more
    severe class wins. Auth dominates because token expiry abort-the-sweep
    is louder than per-offer non-respondable.

    Substring auth-fallback rule: when `codes` is empty (transport-layer
    failure before eBay responds with a parseable body), fall back to
    case-insensitive substring detection on `message`. The
    `auth/token/expir` triggers fire UNLESS the message ALSO contains
    `rate/throttl` (eBay throttle-error messages mention "auth-token rate
    limit"; treating those as auth would trigger spurious sweep aborts).

    Args:
        codes: numeric eBay error codes from `_extract_ebay_error_codes`
            (empty set when the exception has no parseable response).
        message: optional exception message for substring fallback;
            defaults to "" for code-only callers.

    Returns:
        One of "auth" / "non_respondable" / "transport" / "unknown".
    """
    if codes & _AUTH_ERROR_CODES:
        return "auth"
    if codes & _NON_RESPONDABLE_CODES:
        return "non_respondable"
    if codes:
        return "transport"
    lower = message.lower()
    if (
        ("auth" in lower or "token" in lower or "expir" in lower)
        and not ("rate" in lower or "throttl" in lower)
    ):
        return "auth"
    return "unknown"


def _extract_ebay_error_codes(exc: EbaySdkConnectionError) -> set[str]:
    """Pull eBay error codes out of an ebaysdk ConnectionError.

    The exception carries a `.response` whose `.dict()` method exposes a
    `Errors` key (one Error dict OR a list of them). Each Error has an
    `ErrorCode` we use to distinguish "listing already ended" from genuine
    transport failure.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return set()
    try:
        body = response.dict()
    except Exception:  # pragma: no cover — exotic response shape
        return set()
    errors = body.get("Errors")
    if errors is None:
        return set()
    if not isinstance(errors, list):
        errors = [errors]
    codes: set[str] = set()
    for err in errors:
        if isinstance(err, dict):
            code = err.get("ErrorCode")
            if code:
                codes.add(str(code))
    return codes
