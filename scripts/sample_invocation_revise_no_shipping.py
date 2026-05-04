"""Issue #21 Phase 0 — sample invocation for the no-shipping revise payload shape.

Builds a price-only ReviseFixedPriceItem payload via build_revise_payload
(the production function) and asserts the offline contract:

  1. SellerProfiles is present (payment + return profiles attached)
  2. SellerShippingProfile is NOT present (Phase 0 fix)
  3. SellerPaymentProfile + SellerReturnProfile present and populated from .env
  4. Quantity invariant intact (whitebox _assert_no_quantity must not fire)

When invoked with --apply against a real ItemID, also submits the
ReviseFixedPriceItem call and asserts:

  5. eBay Ack ∈ {Success, Warning}
  6. No code 21920361 (auto-mapped to default-shipping)
  7. No code 37 (mixed-mode shipping rejection)

Default mode is OFFLINE (dry-run) — no live eBay submission.
Live mode is opt-in via --apply <ItemID> --confirm.

PREREQUISITES:
  - .env populated with EBAY_PAYMENT_PROFILE_ID + EBAY_RETURN_PROFILE_ID
    (+ EBAY_SHIPPING_PROFILE_ID for env-contract consistency, even though
    Phase 0 no longer attaches it on revise)

USAGE (offline):
  uv run python scripts/sample_invocation_revise_no_shipping.py

USAGE (live revise — destructive; submits a price-only revise):
  uv run python scripts/sample_invocation_revise_no_shipping.py \\
      --apply <ItemID> --price <gbp> --confirm "verified revise no shipping"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from ebay.client import execute_with_retry  # noqa: E402
from ebay.listings import build_revise_payload  # noqa: E402

# eBay Trading API error codes that signal the Phase 0 fix has regressed:
# 21920361 — auto-mapped to default-shipping policy
# 37       — mixed-mode shipping rejection (inline shipping + policy ref)
_REGRESSION_ERROR_CODES = {"21920361", "37"}


def _check_offline_contract(item_id: str, price: float | None) -> dict:
    """Build payload + assert Phase 0 invariants; return the payload."""
    payload = build_revise_payload(item_id=item_id, price=price)
    item = payload["Item"]

    sp = item.get("SellerProfiles")
    assert sp is not None, "SellerProfiles missing from revise payload"

    assert "SellerShippingProfile" not in sp, (
        "FAIL: Phase 0 regression — SellerShippingProfile is attached to revise. "
        "build_revise_payload must call _build_seller_profiles_block(include_shipping=False) "
        "(see feedback_ebay_default_shipping_poisoned.md durable rule)."
    )

    payment = sp.get("SellerPaymentProfile")
    assert payment is not None, "SellerPaymentProfile missing"
    assert payment.get("PaymentProfileID"), "PaymentProfileID empty"

    returns = sp.get("SellerReturnProfile")
    assert returns is not None, "SellerReturnProfile missing"
    assert returns.get("ReturnProfileID"), "ReturnProfileID empty"

    assert "Quantity" not in item, "Quantity leaked into revise payload"

    if price is not None:
        assert item.get("StartPrice") == str(price), (
            f"StartPrice mismatch: expected {price!r}, got {item.get('StartPrice')!r}"
        )

    return payload


def _print_offline_summary(item_id: str, payload: dict) -> None:
    sp = payload["Item"]["SellerProfiles"]
    print("=== Offline payload ===")
    print(f"  ItemID            = {item_id}")
    print(f"  PaymentProfileID  = {sp['SellerPaymentProfile']['PaymentProfileID']}")
    print(f"  ReturnProfileID   = {sp['SellerReturnProfile']['ReturnProfileID']}")
    print("  ShippingProfileID = (NOT attached — Phase 0 contract)")
    print()


async def _live_submit(item_id: str, price: float, payload: dict) -> int:
    print(f"Submitting ReviseFixedPriceItem for ItemID={item_id} price=£{price:.2f}...")
    response = await asyncio.to_thread(execute_with_retry, "ReviseFixedPriceItem", payload)
    reply = response.reply
    ack = getattr(reply, "Ack", None)
    errors = getattr(reply, "Errors", None)

    print(f"\neBay Ack: {ack}")
    regression_hit = False
    if errors is not None:
        if not isinstance(errors, list):
            errors = [errors]
        for err in errors:
            sev = getattr(err, "SeverityCode", "?")
            code = str(getattr(err, "ErrorCode", "?"))
            msg = getattr(err, "LongMessage", getattr(err, "ShortMessage", ""))
            print(f"  [{sev}] {code}: {msg}")
            if code in _REGRESSION_ERROR_CODES:
                regression_hit = True

    if regression_hit:
        print(
            "\nFAIL — eBay returned a Phase 0 regression error code "
            f"({_REGRESSION_ERROR_CODES}). Investigate _build_seller_profiles_block "
            "callers in ebay/listings.py."
        )
        return 1

    if ack in ("Success", "Warning"):
        print("\nPASS — revise without SellerShippingProfile accepted by eBay.")
        return 0
    print(f"\nFAIL — eBay rejected the revise (Ack={ack}).")
    return 1


async def main(args: argparse.Namespace) -> int:
    item_id = args.apply or "111"  # any non-empty placeholder for offline build

    payload = _check_offline_contract(item_id=item_id, price=args.price)
    _print_offline_summary(item_id=item_id, payload=payload)

    if args.apply is None:
        print("Offline contract checks PASSED (dry-run; no eBay call).")
        return 0

    if args.confirm != "verified revise no shipping":
        print(
            "FAIL: --apply requires --confirm \"verified revise no shipping\" "
            "(matches the empirical-revise gate literal in #21 Phase 6).",
            file=sys.stderr,
        )
        return 2

    if args.price is None:
        print("FAIL: --apply requires --price <gbp> for a price-only revise.", file=sys.stderr)
        return 2

    return await _live_submit(item_id=item_id, price=args.price, payload=payload)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Issue #21 Phase 0 — verify build_revise_payload does not attach "
            "SellerShippingProfile."
        ),
    )
    p.add_argument(
        "--apply",
        type=str,
        default=None,
        metavar="ItemID",
        help="Submit a real ReviseFixedPriceItem call against this ItemID. "
        "Default is offline (dry-run) — only payload-shape assertions run.",
    )
    p.add_argument(
        "--price",
        type=float,
        default=None,
        metavar="GBP",
        help="Price (GBP) for the revise. Required when --apply is set.",
    )
    p.add_argument(
        "--confirm",
        type=str,
        default="",
        help='Required when --apply is set: --confirm "verified revise no shipping" '
        "(matches the empirical-revise gate literal).",
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
