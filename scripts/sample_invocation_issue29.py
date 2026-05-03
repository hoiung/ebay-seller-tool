"""Issue #29 — AP #18 sample invocation for the SellerProfiles payload shape.

Builds an AddFixedPriceItem payload via build_add_payload (the production
function) and submits it to eBay's VerifyAddFixedPriceItem endpoint —
identical request shape to AddFixedPriceItem but eBay validates and discards
without creating a listing. This is the canonical eBay-side validation
hook for a payload-shape change, and proves end-to-end that:

  1. Our three Profile IDs from .env exist on the eBay account
  2. The SellerProfiles XML serialises correctly via ebaysdk
  3. eBay accepts the payload shape (no inline ShippingDetails / ReturnPolicy)
  4. Round-tripped via the real production execute_with_retry path

Non-destructive. Safe to re-run.

PREREQUISITES:
  - .env populated with all EBAY_* vars including the three Profile IDs

USAGE:
  uv run python scripts/sample_invocation_issue29.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

from ebay.client import execute_with_retry  # noqa: E402
from ebay.listings import build_add_payload  # noqa: E402


def _sample_specifics() -> dict[str, str | list[str]]:
    """Canonical 21-field item-specifics block (research §1.3)."""
    return {
        "Brand": "Seagate",
        "MPN": "ST2000NX0253",
        "Model": "ST2000NX0253",
        "Product Line": "Enterprise Capacity",
        "Type": "Internal Hard Drive",
        "Drive Type(s) Supported": "HDD",
        "Storage Format": "HDD Only",
        "Storage Capacity": "2TB",
        "Interface": "SATA III",
        "Form Factor": "2.5 in",
        "Height": "15mm",
        "Rotation Speed": "7200 RPM",
        "Cache": "128 MB",
        "Transfer Rate": "6G",
        "Compatible With": "PC",
        "Features": ["Hot Swap", "24/7 Operation"],
        "Colour": "Silver",
        "Country of Origin": "China",
        "EAN": "Does not apply",
        "Manufacturer Warranty": "See Item Description",
        "Unit Type": "Unit",
    }


async def main() -> int:
    payload = build_add_payload(
        title='Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD ST2000NX0253',
        description_html="<html><body><h1>Sample listing — issue #29 verification</h1></body></html>",
        price=49.99,
        quantity=1,
        condition_id=3000,
        condition_description="SMART attributes within spec; no reallocated sectors.",
        item_specifics=_sample_specifics(),
        picture_urls=[
            "https://i.ebayimg.com/images/g/sample1/$_57.JPG",
            "https://i.ebayimg.com/images/g/sample2/$_57.JPG",
        ],
        uuid_hex=uuid.uuid4().hex.upper(),
    )

    item = payload["Item"]
    sp = item.get("SellerProfiles")
    if sp is None:
        print("FAIL: SellerProfiles block missing from payload", file=sys.stderr)
        return 1

    # Pre-flight assertions — confirm payload shape before going to eBay.
    assert "ShippingDetails" not in item, "inline ShippingDetails present (must be SellerProfiles)"
    assert "ReturnPolicy" not in item, "inline ReturnPolicy present (must be SellerProfiles)"
    assert "PaymentMethods" not in item, "inline PaymentMethods present (must be SellerProfiles)"

    print("=== Payload SellerProfiles ===")
    print(f"  PaymentProfileID  = {sp['SellerPaymentProfile']['PaymentProfileID']}")
    print(f"  ShippingProfileID = {sp['SellerShippingProfile']['ShippingProfileID']}")
    print(f"  ReturnProfileID   = {sp['SellerReturnProfile']['ReturnProfileID']}")
    print()
    print("Submitting to VerifyAddFixedPriceItem (no listing created)...")

    response = await asyncio.to_thread(execute_with_retry, "VerifyAddFixedPriceItem", payload)
    reply = response.reply
    ack = getattr(reply, "Ack", None)
    errors = getattr(reply, "Errors", None)

    print(f"\neBay Ack: {ack}")
    if errors is not None:
        if not isinstance(errors, list):
            errors = [errors]
        for err in errors:
            sev = getattr(err, "SeverityCode", "?")
            code = getattr(err, "ErrorCode", "?")
            msg = getattr(err, "LongMessage", getattr(err, "ShortMessage", ""))
            print(f"  [{sev}] {code}: {msg}")

    if ack in ("Success", "Warning"):
        print("\nPASS — eBay accepted the SellerProfiles payload shape.")
        return 0
    print(f"\nFAIL — eBay rejected the payload (Ack={ack}).")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
