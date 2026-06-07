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
    """Synthetic 20-key item-specifics block — proves the payload shape with NO
    product data (the public repo ships none)."""
    return {
        "Brand": "Fabrikam",
        "MPN": "FBKM-ALPHA-01",
        "Model": "FBKM-ALPHA-01",
        "Product Family": "Northwind Alpha",
        "Widget Type": "Synthetic Widget",
        "Medium Class": "Class-Z",
        "Packaging": "Bare Unit",
        "Capacity Spec": "2TB",
        "Bus Spec": "Synthetic-Bus III",
        "Body Size": "2.5 in",
        "Spin Spec": "7200 RPM",
        "Buffer Spec": "128 MB",
        "Link Rate": "RATE-MID",
        "Fits With": "Generic Host",
        "Traits": ["Trait-Alpha", "Trait-Beta"],
        "Shade": "Greyish",
        "Origin Mark": "Atlantis",
        "Barcode": "Does not apply",
        "Cover Note": "See Item Description",
        "Pack Unit": "Unit",
    }


async def main() -> int:
    payload = build_add_payload(
        title="Fabrikam Northwind Alpha 2TB 7200RPM 15mm Widget FBKM-ALPHA-01",
        description_html=(
            "<html><body><h1>Sample listing — issue #29 verification</h1></body></html>"
        ),
        price=49.99,
        quantity=1,
        condition_id=3000,
        condition_description="Diagnostics within spec; no faults.",
        item_specifics=_sample_specifics(),
        category_id="CONTRACT-CAT-0001",
        picture_urls=[
            "https://i.ebayimg.com/images/g/sample1/$_57.JPG",
            "https://i.ebayimg.com/images/g/sample2/$_57.JPG",
        ],
        uuid_hex=uuid.uuid4().hex.upper(),
    )

    item = payload["Item"]

    # #29-followup permanent fix: NO SellerProfiles block emitted EVER —
    # shipping is inline (FreeShipping=true) and account-level eBay Simple
    # Delivery is the source of truth. Code never attaches policies.
    assert "SellerProfiles" not in item, (
        "FAIL: SellerProfiles attached to AddFixedPriceItem — would let "
        "eBay auto-fill account-default shipping, destroying inline free "
        "config. See module-level 'SellerProfiles attachment policy' "
        "docstring in ebay/listings.py."
    )
    assert "ShippingDetails" in item, "inline ShippingDetails missing"
    assert "ReturnPolicy" not in item, "inline ReturnPolicy present"
    assert "PaymentMethods" not in item, "inline PaymentMethods present"
    assert item["ShippingDetails"]["ShippingServiceOptions"]["FreeShipping"] == "true", (
        "FreeShipping must be true (seller-pays default)"
    )

    print("=== Payload (NO SellerProfiles attached) ===")
    print(f"  Item dict keys = {sorted(item.keys())}")
    print("  Inline shipping: FreeShipping=true, UK Royal Mail 2nd Class, £0.00")
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
        print("\nPASS — eBay accepted the no-SellerProfiles AddItem payload.")
        return 0
    print(f"\nFAIL — eBay rejected the payload (Ack={ack}).")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
