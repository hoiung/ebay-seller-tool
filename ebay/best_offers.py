"""eBay Trading API wrappers for Best Offer state transitions.

Two verbs only:
    GetBestOffers        — read pending offers across all listings (per-seller)
    RespondToBestOffer   — accept / counter / decline a single offer

Carve-out (Issue #14 Phase 3 pattern, replicated in #16): `execute_with_retry`
is lazy-imported INSIDE each wrapper body so unit tests can patch the API
surface (via `patch("ebay.client.execute_with_retry")`) without dragging
the broader analytics layer into the mock graph. Documented per-call via
`# noqa: PLC0415`. Note (Issue #33): the module-top `from .listings import
_decimal_str` import does transitively load `ebay.client` (listings.py uses
`log_debug` at module-top), but the `execute_with_retry` lazy-import seam
remains intact — the test-patch pattern still works correctly because
patches resolve against `ebay.client` symbols directly, not through the
import chain.

Used by:
    - .claude/skills/ebay-seller-tool/scripts/respond_best_offers.py
      (the autonomous responder script — Issue #16 Phase 3)

NOT used by the public MCP tool surface — the responder is a local
script + ebaysdk direct call, NOT a registered MCP tool (per #16 locked
constraint #4: NO new MCP tool).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from .listings import _decimal_str

# Action enum for respond_to_best_offer — Literal type narrows callers.
BestOfferAction = Literal["Accept", "Counter", "Decline"]

# eBay Trading API enum values for the GetBestOffers `BestOfferStatus` REQUEST
# filter. Stage 5 R3 fix — was "Pending" which eBay rejects with code 20139
# "Invalid BestOfferStatus. BestOfferStatus is Invalid. Valid values are All,
# Active." Note: individual <BestOffer> nodes in the RESPONSE carry their own
# BestOfferStatus="Pending" field — that's a separate enum from the request
# filter. "Active" semantically means "still pending, not yet responded or
# expired" which is what we want.
_ACTIVE_FILTER = "Active"


def _as_list(node: Any) -> list:
    """Coerce a single ebaysdk node OR a list to a list. Mirrors selling.py."""
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


def _coerce_quantity(raw: Any) -> int:
    """Coerce eBay's <Quantity> XML field to a positive int, defaulting to 1.

    eBay's `<Quantity>` field on `<BestOffer>` nodes is documented as int,
    but ebaysdk decodes XML scalars as strings on some response paths; mirror
    the defensive `buyer_offer_gbp` pattern at line ~62 (str → float coerce).
    None / missing / empty / unparseable → default to 1 (single-qty offer).
    Field name verified by Issue #30 AC5.1 read-only live probe.
    """
    if raw is None or raw == "":
        return 1
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 1
    return val if val >= 1 else 1


def _is_buyer_actionable(parsed: dict[str, Any]) -> bool:
    """Allowlist filter — only `BuyerBestOffer` rows are actionable by the seller.

    eBay's `BestOfferCodeType` enum is documented {`BuyerBestOffer`,
    `SellerCounterOffer`, `AdminCounterOffer`} per
    developer.ebay.com/devzone/xml/docs/reference/ebay/GetBestOffers.html.
    Only `BuyerBestOffer` is buyer-initiated and seller-actionable;
    `RespondToBestOffer` against `SellerCounterOffer` / `AdminCounterOffer`
    fires eBay error 21940 ("Cannot respond to your own counter offer").
    Allowlist (==) is forward-safe — any future enum value defaults to
    "drop", erring on the side of "don't act" rather than "act with
    unknown contract". See Issue #32 D7 root-cause fix.
    """
    return parsed.get("best_offer_code_type") == "BuyerBestOffer"


def _parse_offer_node(offer_node: Any, item_id: str) -> dict[str, Any]:
    """Extract the structured offer fields from an ebaysdk BestOffer node.

    Returns a dict with the 9 fields the responder + JSONL ledger need.
    Tolerant to missing fields (defaults to safe sentinels) since eBay
    responses can omit optional fields. Issue #30 AC1.3 added `quantity`
    so the responder can dispatch to the matching qty_tier; field name
    `Quantity` verified by AC5.1 live probe — see
    docs/research/ebay/11_EBAY_API_AND_MCP_SERVER.md.
    """
    buyer_node = getattr(offer_node, "Buyer", None)
    price_node = getattr(offer_node, "Price", None)
    return {
        "offer_id": str(getattr(offer_node, "BestOfferID", "") or ""),
        "item_id": str(item_id or ""),
        "buyer_user_id": str(getattr(buyer_node, "UserID", "") or "") if buyer_node else "",
        "buyer_offer_gbp": float(getattr(price_node, "value", 0.0) or 0.0) if price_node else 0.0,
        "buyer_message": str(getattr(offer_node, "BuyerMessage", "") or ""),
        "offer_timestamp_iso": str(getattr(offer_node, "ReceivedTime", "") or ""),
        "expiration_iso": str(getattr(offer_node, "ExpirationTime", "") or ""),
        "best_offer_code_type": str(getattr(offer_node, "BestOfferCodeType", "") or ""),
        "quantity": _coerce_quantity(getattr(offer_node, "Quantity", None)),
    }


async def get_pending_best_offers(
    item_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch all `BestOfferStatus=Active` offers for the seller's listings.

    Two modes (Issue #30 AC3.1):
        - **Per-seller mode** (`item_ids=None`, default): single
          `GetBestOffers(BestOfferStatus=Active)` call (no ItemID filter).
          Backward-compat for existing unit tests; documented quirks under
          this mode silently drop some offers (cross-border buyer poll
          divergence — Issue #30 root cause).
        - **Per-item mode** (`item_ids=[...]`, production path post-#30): one
          `GetBestOffers(ItemID=..., BestOfferStatus=Active)` call per listing.
          Has full documented BestOfferStatus semantics + verified lived
          behaviour against real cross-border offers.

    Per-seller response shape: <ItemArray><Item><ItemID>…<BestOfferArray><BestOffer>…
    Per-item response shape:   <BestOfferArray><BestOffer>… directly under root
                               (no <ItemArray> wrapper; AC5.1 live-probe
                               verified).

    Returns:
        list[dict]: each dict has 9 fields per `_parse_offer_node` (8 +
        quantity). Empty list (NOT None) when no pending offers — keeps the
        caller's iteration simple.
    """
    from ebay.client import execute_with_retry, log_debug  # noqa: PLC0415

    if item_ids is None:
        # Backward-compat per-seller mode — single API call.
        log_debug("get_pending_best_offers: polling per-seller GetBestOffers")
        response = await asyncio.to_thread(
            execute_with_retry,
            "GetBestOffers",
            {"BestOfferStatus": _ACTIVE_FILTER, "DetailLevel": "ReturnAll"},
        )

        item_array = getattr(response.reply, "ItemArray", None)
        if item_array is None:
            return []

        offers: list[dict[str, Any]] = []
        for item_node in _as_list(getattr(item_array, "Item", None)):
            item_id = str(getattr(item_node, "ItemID", "") or "")
            bo_array = getattr(item_node, "BestOfferArray", None)
            if bo_array is None:
                continue
            for offer_node in _as_list(getattr(bo_array, "BestOffer", None)):
                parsed = _parse_offer_node(offer_node, item_id)
                if not _is_buyer_actionable(parsed):
                    continue
                offers.append(parsed)

        log_debug(
            f"get_pending_best_offers: returned {len(offers)} pending offer(s) (per-seller mode)"
        )
        return offers

    # Per-item iteration mode — one GetBestOffers call per listing ID.
    from ebay.client import log_warn  # noqa: PLC0415

    # Issue #32 Phase 2+4 — _AUTH_ERROR_CODES + _classify_ebay_error_codes
    # sourced from end_listing.py (canonical single location across both
    # consumers). Mini-Stage-5 GAP-C behaviour preserved: per-item sweep
    # MUST raise on auth-error rather than log+continue, so expired tokens
    # abort loud-and-fast instead of silently producing a misleading
    # "errors=22 offers_found=0" log line that looks like a quiet day.
    from ebay.end_listing import (  # noqa: PLC0415
        _AUTH_ERROR_CODES,
        _classify_ebay_error_codes,
        _extract_ebay_error_codes,
    )

    log_debug(
        f"get_pending_best_offers: polling per-item GetBestOffers ({len(item_ids)} listing(s))"
    )
    offers: list[dict[str, Any]] = []
    skipped_no_offers = 0
    # Issue #32 Phase 3 — replace single `skipped_errors` counter with 4-way
    # split via _classify_ebay_error_codes. Auth path raises (sweep abort),
    # so errors_auth + errors_non_respondable would be 0 at emit time on the
    # GET path; tracked anyway for schema-symmetry with the responder script.
    errors_auth = 0
    errors_non_respondable = 0
    errors_transport = 0
    errors_other = 0
    sweep_start_ns = time.monotonic_ns()
    for item_id in item_ids:
        if not item_id:
            continue
        try:
            response = await asyncio.to_thread(
                execute_with_retry,
                "GetBestOffers",
                {
                    "ItemID": str(item_id),
                    "BestOfferStatus": _ACTIVE_FILTER,
                    "DetailLevel": "ReturnAll",
                },
            )
        except Exception as e:  # noqa: BLE001 — defensive: eBay raises ebaysdk.ConnectionError
            # Mini-Stage-5 GAP-A fix: extract eBay error codes via the canonical
            # `_extract_ebay_error_codes` helper (matches `respond_best_offers.py`
            # auth-detection pattern). Substring-on-message was brittle: hypothetical
            # codes 220140 / 201400 would false-positive against "20140 in err_text",
            # and the English "Best Offers Not Found" fallback breaks if eBay ever
            # localises the message text.
            ebay_codes = _extract_ebay_error_codes(e) if isinstance(e, Exception) else set()
            err_text = str(e)

            # Mini-Stage-5 GAP-C fix: auth-token errors must ABORT the sweep,
            # not silently skip 22 items. Token expiry would otherwise produce
            # a "polled=22 errors=22 offers_found=0" log line that looks like
            # a quiet day to a casual operator. Mirror the responder's
            # `_AUTH_ERROR_CODES` set + propagate via raise so main_async's
            # outer try/except (respond_best_offers.py:617-623) detects + paged.
            if ebay_codes & _AUTH_ERROR_CODES:
                log_warn(
                    f"get_pending_best_offers: AUTH ERROR on ItemID={item_id} "
                    f"(eBay codes={sorted(ebay_codes)}); aborting sweep"
                )
                raise

            # Code 20140 ("Best Offers Not Found / No best offers found for
            # your criteria") fires for every listing with zero active offers.
            # ebaysdk surfaces it as a raised ConnectionError, not a clean
            # empty response. Treat as the empty-set signal it actually is.
            #
            # Stage 5 finding A9 + Mini-Stage-5 GAP-A: code-set match replaces
            # substring (defence-in-depth + locale-independent).
            if "20140" in ebay_codes or "Best Offers Not Found" in err_text:
                skipped_no_offers += 1
                continue
            # Other errors (non-auth, non-20140 transport / 5xx / unexpected):
            # log + skip this item; the next 30-min cron cycle re-polls from
            # eBay's truth. Burning the whole sweep on one transient 5xx would
            # punish 21 listings for one item's hiccup.
            #
            # Issue #32 Phase 3+4 — bucket via _classify_ebay_error_codes so
            # the per-sweep stats line exposes auth/non_respondable/transport/
            # other counts separately. Auth would have raised above (sweep
            # abort), so this branch only sees non-auth classes.
            err_class = _classify_ebay_error_codes(ebay_codes, err_text)
            if err_class == "auth":
                # Defence-in-depth — auth detected via substring fallback
                # (no codes parsed). Mirror the codes-path: abort the sweep.
                log_warn(
                    f"get_pending_best_offers: AUTH ERROR on ItemID={item_id} "
                    f"(substring-detected, no parseable codes); aborting sweep"
                )
                raise
            if err_class == "non_respondable":
                errors_non_respondable += 1
            elif err_class == "transport":
                errors_transport += 1
            else:
                errors_other += 1
            log_warn(
                f"get_pending_best_offers: per-item poll failed for ItemID={item_id} "
                f"class={err_class} ({type(e).__name__}: {err_text[:200]}); skipping this listing"
            )
            continue

        # Per-item shape: <BestOfferArray><BestOffer>… directly under reply.
        # ebaysdk may also nest under <ItemArray><Item><BestOfferArray>… on
        # some endpoints — handle both shapes defensively.
        bo_array = getattr(response.reply, "BestOfferArray", None)
        if bo_array is not None:
            for offer_node in _as_list(getattr(bo_array, "BestOffer", None)):
                parsed = _parse_offer_node(offer_node, str(item_id))
                if not _is_buyer_actionable(parsed):
                    continue
                offers.append(parsed)
            continue

        item_array = getattr(response.reply, "ItemArray", None)
        if item_array is None:
            continue
        for item_node in _as_list(getattr(item_array, "Item", None)):
            inner_bo = getattr(item_node, "BestOfferArray", None)
            if inner_bo is None:
                continue
            for offer_node in _as_list(getattr(inner_bo, "BestOffer", None)):
                parsed = _parse_offer_node(offer_node, str(item_id))
                if not _is_buyer_actionable(parsed):
                    continue
                offers.append(parsed)

    # Issue #32 Phase 3 — emit 8-key stats line unconditionally for audit
    # symmetry + observability of D5/D7 axes. Replaces the legacy 4-key
    # (polled/no_offers/errors/offers_found) format with the
    # auth/non_respondable/transport/other split. wall_clock_ms via
    # time.monotonic_ns() delta — operator's signal for sweep-duration
    # regressions on the per-item path. Fixed-cost log line (1 per cron tick).
    wall_clock_ms = (time.monotonic_ns() - sweep_start_ns) // 1_000_000
    log_debug(
        f"get_pending_best_offers: per-item sweep stats — "
        f"polled={len(item_ids)} no_offers={skipped_no_offers} "
        f"errors_auth={errors_auth} errors_non_respondable={errors_non_respondable} "
        f"errors_transport={errors_transport} errors_other={errors_other} "
        f"offers_found={len(offers)} wall_clock_ms={wall_clock_ms}"
    )

    log_debug(f"get_pending_best_offers: returned {len(offers)} pending offer(s) (per-item mode)")
    return offers


async def respond_to_best_offer(
    item_id: str,
    offer_id: str,
    action: BestOfferAction,
    counter_price_gbp: float | None = None,
    counter_quantity: int | None = None,
) -> dict[str, Any]:
    """Accept / counter / decline a single Best Offer.

    Args:
        item_id: eBay ItemID of the listing the offer was made on.
        offer_id: eBay BestOfferID returned by `get_pending_best_offers`.
        action: Literal "Accept" | "Counter" | "Decline".
        counter_price_gbp: REQUIRED when action="Counter"; ignored otherwise.
            Raises ValueError if action="Counter" and value is None or <= 0.
        counter_quantity: REQUIRED when action="Counter"; ignored otherwise.
            eBay Trading API CounterOfferQuantity field (Issue #33 Stage 5
            fix — Code 21921 "counteroffer quantity is required" surfaced
            in production after the Phase 1 Code 5 XML Parse fix exposed
            eBay's next-layer validation). Raises ValueError if
            action="Counter" and value is None or <= 0.

    Returns:
        dict with 4 fields:
            - success: bool — True when eBay returned no error
            - ebay_response_status: str — eBay's Ack ("Success" / "Warning" / "Failure")
            - ebay_response_code: str | None — error code if Failure (e.g. "21916")
            - error_message: str | None — None on success; populated on error

    Raises:
        ValueError: if action="Counter" without positive counter_price_gbp
            OR positive counter_quantity.

    NOTE: All eBay-side errors (BestOffer-disabled, MinimumBestOfferPrice
    violation, stale state, 5-counter aggregate limit) surface as
    `ebaysdk.exceptions.ConnectionError` from execute_with_retry. The CALLER
    (responder script) catches per-offer per AC3.5 and continues the loop.
    """
    from ebay.client import execute_with_retry, log_debug  # noqa: PLC0415

    if action == "Counter":
        if counter_price_gbp is None or counter_price_gbp <= 0:
            raise ValueError(
                f"action='Counter' requires positive counter_price_gbp; got {counter_price_gbp!r}"
            )
        if counter_quantity is None or counter_quantity <= 0:
            raise ValueError(
                f"action='Counter' requires positive counter_quantity; got {counter_quantity!r}"
            )

    payload: dict[str, Any] = {
        "ItemID": item_id,
        "BestOfferID": offer_id,
        "Action": action,
    }
    if action == "Counter":
        # ebaysdk 2.2.0 dict→XML serialiser: `{"#text": V, "@attrs": {"currencyID": C}}`
        # emits `<X currencyID="C">V</X>`. Matches the canonical shape used at
        # listings.py:491-502 for BestOfferAutoAcceptPrice + MinimumBestOfferPrice
        # (single-source-edit per AP #9). _decimal_str uses
        # Decimal(str(value)).quantize(Decimal("0.01")) for float-drift safety.
        payload["CounterOfferPrice"] = {
            "#text": _decimal_str(counter_price_gbp),
            "@attrs": {"currencyID": "GBP"},
        }
        # CounterOfferQuantity is a plain int element (no currency attribute);
        # required by eBay even for qty=1 listings (Issue #33 Stage 5 fix —
        # Code 21921 surfaced live 2026-05-09T15:16Z after Phase 1 closed Code 5).
        payload["CounterOfferQuantity"] = int(counter_quantity)

    log_debug(
        f"respond_to_best_offer: item={item_id} offer={offer_id} action={action} "
        f"counter={counter_price_gbp} qty={counter_quantity}"
    )

    response = await asyncio.to_thread(execute_with_retry, "RespondToBestOffer", payload)

    # ebaysdk response.reply has .Ack ("Success" / "Warning" / "Failure") + optional Errors.
    ack = str(getattr(response.reply, "Ack", "") or "")
    errors_node = getattr(response.reply, "Errors", None)
    error_code: str | None = None
    error_message: str | None = None
    if errors_node is not None:
        # First error wins for the audit ledger (most informative).
        first_err = errors_node[0] if isinstance(errors_node, list) else errors_node
        error_code = str(getattr(first_err, "ErrorCode", "") or "") or None
        error_message = str(getattr(first_err, "LongMessage", "") or "") or None

    success = ack in ("Success", "Warning") and error_message is None
    return {
        "success": success,
        "ebay_response_status": ack,
        "ebay_response_code": error_code,
        "error_message": error_message,
    }
