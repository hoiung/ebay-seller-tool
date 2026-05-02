"""eBay Trading API wrappers for Best Offer state transitions.

Two verbs only:
    GetBestOffers        — read pending offers across all listings (per-seller)
    RespondToBestOffer   — accept / counter / decline a single offer

Carve-out (Issue #14 Phase 3 pattern, replicated in #16): module-top imports
deliberately exclude `ebay.client` (`execute_with_retry`, `log_*`). Each
wrapper lazy-imports them INSIDE its own body so unit tests can patch the
API surface without dragging the broader analytics + listings layer into
the mock graph. Documented per-call via `# noqa: PLC0415`.

Used by:
    - .claude/skills/ebay-seller-tool/scripts/respond_best_offers.py
      (the autonomous responder script — Issue #16 Phase 3)

NOT used by the public MCP tool surface — the responder is a local
script + ebaysdk direct call, NOT a registered MCP tool (per #16 locked
constraint #4: NO new MCP tool).
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

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


def _parse_offer_node(offer_node: Any, item_id: str) -> dict[str, Any]:
    """Extract the structured offer fields from an ebaysdk BestOffer node.

    Returns a dict with the 8 fields the responder + JSONL ledger need.
    Tolerant to missing fields (defaults to safe sentinels) since eBay
    responses can omit optional fields.
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
    }


async def get_pending_best_offers() -> list[dict[str, Any]]:
    """Fetch all `BestOfferStatus=Pending` offers across the seller's listings.

    Single API call (no `ItemID` → per-seller scope). Returns a flat list of
    offer dicts. Empty list (NOT None) when no pending offers — keeps the
    caller's iteration simple.

    eBay Trading API behaviour:
        - Per-seller mode (no ItemID) returns up to 10,000 offer IDs in one
          response — well above our 22-listing × 3-buyer-rounds × ~1 ceiling.
        - Each <BestOffer> element appears nested under <Item><BestOfferArray>
          when ItemID specified, OR in a flat array at the top of the
          response in per-seller mode.
        - We iterate <Item> nodes (per-seller mode) and collect their
          <BestOfferArray><BestOffer> children, tagging each with the item_id
          for the responder + audit ledger.

    Returns:
        list[dict]: each dict has 8 fields per `_parse_offer_node`.
    """
    from ebay.client import execute_with_retry, log_debug  # noqa: PLC0415

    log_debug("get_pending_best_offers: polling per-seller GetBestOffers")
    response = await asyncio.to_thread(
        execute_with_retry,
        "GetBestOffers",
        {"BestOfferStatus": _ACTIVE_FILTER, "DetailLevel": "ReturnAll"},
    )

    # Per-seller response shape: <ItemArray><Item><ItemID>...<BestOfferArray><BestOffer>...
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
            offers.append(_parse_offer_node(offer_node, item_id))

    log_debug(f"get_pending_best_offers: returned {len(offers)} pending offer(s)")
    return offers


async def respond_to_best_offer(
    item_id: str,
    offer_id: str,
    action: BestOfferAction,
    counter_price_gbp: float | None = None,
) -> dict[str, Any]:
    """Accept / counter / decline a single Best Offer.

    Args:
        item_id: eBay ItemID of the listing the offer was made on.
        offer_id: eBay BestOfferID returned by `get_pending_best_offers`.
        action: Literal "Accept" | "Counter" | "Decline".
        counter_price_gbp: REQUIRED when action="Counter"; ignored otherwise.
            Raises ValueError if action="Counter" and value is None or <= 0.

    Returns:
        dict with 4 fields:
            - success: bool — True when eBay returned no error
            - ebay_response_status: str — eBay's Ack ("Success" / "Warning" / "Failure")
            - ebay_response_code: str | None — error code if Failure (e.g. "21916")
            - error_message: str | None — None on success; populated on error

    Raises:
        ValueError: if action="Counter" without a positive counter_price_gbp.

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

    payload: dict[str, Any] = {
        "ItemID": item_id,
        "BestOfferID": offer_id,
        "Action": action,
    }
    if action == "Counter":
        payload["CounterOfferPrice"] = {"value": counter_price_gbp, "@currencyID": "GBP"}

    log_debug(
        f"respond_to_best_offer: item={item_id} offer={offer_id} action={action} "
        f"counter={counter_price_gbp}"
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
