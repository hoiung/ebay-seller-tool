"""
Listing data helpers shared by MCP tools and skill scripts.

Contains the canonical listing-to-dict serialisation, diff computation,
payload building, and audit log writer. Single source of truth — no
duplicate serialisation logic elsewhere.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ebay.client import log_debug


def listing_to_dict(item: object) -> dict:
    """Convert an ebaysdk Item response object to a plain dict.

    Uses hasattr guards on all attributes (ebaysdk returns dynamic objects
    that may or may not have fields depending on the API call and listing state).

    This is the SINGLE serialisation path — both MCP tools and skill scripts
    must use this function (DRY).
    """
    # Item specifics — flatten NameValueList into dict[name -> list[values]]
    specifics: dict[str, list[str]] = {}
    if hasattr(item, "ItemSpecifics") and item.ItemSpecifics is not None:
        name_value = item.ItemSpecifics.NameValueList
        if not isinstance(name_value, list):
            name_value = [name_value]
        for nv in name_value:
            name = str(nv.Name)
            value = nv.Value
            if isinstance(value, list):
                specifics[name] = [str(v) for v in value]
            else:
                specifics[name] = [str(value)]

    description = ""
    if hasattr(item, "Description") and item.Description is not None:
        description = str(item.Description)

    # Photo URLs
    photos: list[str] = []
    if hasattr(item, "PictureDetails") and item.PictureDetails is not None:
        pic_url = getattr(item.PictureDetails, "PictureURL", None)
        if pic_url is not None:
            if isinstance(pic_url, list):
                photos = [str(u) for u in pic_url]
            else:
                photos = [str(pic_url)]

    return {
        "item_id": str(item.ItemID),
        "title": str(item.Title),
        "subtitle": (str(item.SubTitle) if hasattr(item, "SubTitle") and item.SubTitle else None),
        "condition_id": (
            str(item.ConditionID) if hasattr(item, "ConditionID") and item.ConditionID else None
        ),
        "condition_name": (
            str(item.ConditionDisplayName)
            if hasattr(item, "ConditionDisplayName") and item.ConditionDisplayName
            else None
        ),
        "primary_category_id": (
            str(item.PrimaryCategory.CategoryID) if hasattr(item, "PrimaryCategory") else None
        ),
        "primary_category_name": (
            str(item.PrimaryCategory.CategoryName) if hasattr(item, "PrimaryCategory") else None
        ),
        "price": str(item.SellingStatus.CurrentPrice.value),
        "currency": str(item.SellingStatus.CurrentPrice._currencyID),
        "quantity": int(item.Quantity),
        "quantity_available": (
            int(item.QuantityAvailable) if hasattr(item, "QuantityAvailable") else None
        ),
        "watch_count": int(getattr(item, "WatchCount", 0) or 0),
        "view_count": int(getattr(item, "HitCount", 0) or 0),
        "listing_url": str(item.ListingDetails.ViewItemURL),
        "specifics": specifics,
        "description_html": description,
        "description_length": len(description),
        "photos": photos,
    }


def snapshot_listing(item: object) -> dict:
    """Create a lightweight snapshot of a listing for before/after comparison."""
    d = listing_to_dict(item)
    return {
        "item_id": d["item_id"],
        "title": d["title"],
        "price": d["price"],
        "description_length": d["description_length"],
        "description_hash": hashlib.sha256(d["description_html"].encode()).hexdigest()[:16],
        "quantity": d["quantity"],
    }


def compute_diff(
    before: dict,
    title: str | None,
    description_html: str | None,
    price: float | None,
) -> dict:
    """Compute which fields would change and their before/after values."""
    diff: dict[str, dict] = {}
    if title is not None and title != before.get("title"):
        diff["title"] = {"before": before.get("title"), "after": title}
    if description_html is not None:
        before_hash = before.get("description_hash", "")
        after_hash = hashlib.sha256(description_html.encode()).hexdigest()[:16]
        if before_hash != after_hash:
            diff["description_html"] = {
                "before_length": before.get("description_length"),
                "after_length": len(description_html),
                "before_hash": before_hash,
                "after_hash": after_hash,
            }
    if price is not None:
        # Compare as floats to avoid false positives (eBay stores "10.00", Python str(10.0)="10.0")
        try:
            before_price = float(before.get("price", 0))
        except (ValueError, TypeError):
            before_price = 0.0
        if abs(price - before_price) > 0.001:
            diff["price"] = {"before": before.get("price"), "after": str(price)}
    return diff


def extract_shipping_details(item: object) -> dict:
    """Extract shipping config from a GetItem response for echo-back on revision.

    eBay requires ShippingDetails on every ReviseFixedPriceItem call. This
    extracts the current config so we can echo it back without overwriting it
    with a hardcoded default.
    """
    if not hasattr(item, "ShippingDetails") or item.ShippingDetails is None:
        # Fallback if no shipping info returned (shouldn't happen for active listings)
        return {
            "ShippingType": "Flat",
            "ShippingServiceOptions": {
                "ShippingServicePriority": "1",
                "ShippingService": "UK_RoyalMailSecondClassStandard",
                "ShippingServiceCost": "0.00",
                "FreeShipping": "true",
            },
        }

    sd = item.ShippingDetails
    result: dict = {}
    if hasattr(sd, "ShippingType") and sd.ShippingType:
        result["ShippingType"] = str(sd.ShippingType)

    # Extract domestic shipping service options
    if hasattr(sd, "ShippingServiceOptions") and sd.ShippingServiceOptions is not None:
        sso = sd.ShippingServiceOptions
        if not isinstance(sso, list):
            sso = [sso]
        options = []
        for s in sso:
            opt: dict = {}
            for attr in ["ShippingService", "ShippingServicePriority", "FreeShipping"]:
                val = getattr(s, attr, None)
                if val is not None:
                    opt[attr] = str(val).lower() if isinstance(val, bool) else str(val)
            # Cost fields are Amount objects with .value attribute
            for cost_attr in ["ShippingServiceCost", "ShippingServiceAdditionalCost"]:
                val = getattr(s, cost_attr, None)
                if val is not None:
                    opt[cost_attr] = str(getattr(val, "value", val))
            if opt:
                options.append(opt)
        if options:
            result["ShippingServiceOptions"] = options if len(options) > 1 else options[0]

    # If we couldn't extract anything useful, use the safe default
    if not result or "ShippingServiceOptions" not in result:
        return {
            "ShippingType": "Flat",
            "ShippingServiceOptions": {
                "ShippingServicePriority": "1",
                "ShippingService": "UK_RoyalMailSecondClassStandard",
                "ShippingServiceCost": "0.00",
                "FreeShipping": "true",
            },
        }

    return result


def cdata_wrap(html: str) -> str:
    """Wrap HTML in CDATA for eBay XML payload. Handles ]]> in content."""
    # eBay XML-escapes HTML unless it's in CDATA. Handle the edge case where
    # the HTML itself contains ]]> by splitting and re-opening CDATA sections.
    escaped = html.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{escaped}]]>"


def build_revise_payload(
    item_id: str,
    title: str | None = None,
    description_html: str | None = None,
    price: float | None = None,
    shipping_details: dict | None = None,
) -> dict:
    """Build the ReviseFixedPriceItem payload dict.

    NEVER includes a Quantity key at any nesting level — this is a safety
    invariant verified by whitebox test.

    shipping_details: optional dict to echo back the listing's current shipping
    config. eBay requires shipping info on ReviseFixedPriceItem even for
    description-only updates. If None, a default free UK Royal Mail 2nd Class
    config is used (all our listings are free domestic shipping).
    """
    item: dict = {"ItemID": item_id}
    if title is not None:
        item["Title"] = title
    if description_html is not None:
        item["Description"] = cdata_wrap(description_html)
    if price is not None:
        item["StartPrice"] = str(price)

    # eBay requires ShippingDetails on every ReviseFixedPriceItem call
    if shipping_details is not None:
        item["ShippingDetails"] = shipping_details
    else:
        item["ShippingDetails"] = {
            "ShippingType": "Flat",
            "ShippingServiceOptions": {
                "ShippingServicePriority": "1",
                "ShippingService": "UK_RoyalMailSecondClassStandard",
                "ShippingServiceCost": "0.00",
                "FreeShipping": "true",
            },
        }

    payload = {"Item": item}

    # Whitebox safety: verify no Quantity key leaked in
    _assert_no_quantity(payload)

    return payload


def _assert_no_quantity(d: dict | list, path: str = "") -> None:
    """Recursively verify no Quantity key exists in the payload."""
    if isinstance(d, dict):
        for k, v in d.items():
            if k.lower() == "quantity":
                raise ValueError(f"SAFETY: Quantity key found at {path}.{k} — refusing to build")
            _assert_no_quantity(v, f"{path}.{k}")
    elif isinstance(d, list):
        for i, v in enumerate(d):
            _assert_no_quantity(v, f"{path}[{i}]")


# --- Audit log ---

_AUDIT_LOG_DIR = Path.home() / ".local" / "share" / "ebay-seller-tool"
_AUDIT_LOG_PATH = _AUDIT_LOG_DIR / "audit.log"


def audit_log_write(
    item_id: str,
    fields_changed: list[str],
    before_length: int,
    after_length: int,
    success: bool,
    error: str | None = None,
    local_html_path: str | None = None,
) -> None:
    """Append one JSON line to the audit log.

    Disk-full: catches OSError, logs to stderr via log_debug. The eBay update
    is NOT rolled back for a log failure — but the caller is informed via
    the log_debug warning.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "item_id": item_id,
        "fields_changed": fields_changed,
        "before_length": before_length,
        "after_length": after_length,
        "success": success,
        "error": error,
        "local_html_path": local_html_path,
    }
    try:
        _AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log_debug(f"AUDIT_LOG_WRITE_FAILED error={e} — update was applied but log entry lost")


def extract_warning_block(html: str) -> str | None:
    """Extract the first warning div block from HTML content.

    Returns the full <div class="warning"...>...</div> block, or None if
    no warning block found.
    """
    # Match the outermost warning div — use a non-greedy approach with
    # nested div awareness (count opening/closing div tags)
    match = re.search(r'<div\b[^>]*\bclass=["\']warning["\']', html)
    if not match:
        return None

    start = match.start()
    depth = 0
    i = start
    while i < len(html):
        if html[i:].startswith("<div"):
            depth += 1
            i += 4
        elif html[i:].startswith("</div>"):
            depth -= 1
            if depth == 0:
                return html[start : i + 6]
            i += 6
        else:
            i += 1
    return None
