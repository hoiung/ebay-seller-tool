"""
Listing data helpers shared by MCP tools and skill scripts.

Contains the canonical listing-to-dict serialisation, diff computation,
payload building, and audit log writer. Single source of truth — no
duplicate serialisation logic elsewhere.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ebay.client import log_debug

_UUID_RE = re.compile(r"^[0-9A-F]{32}$")
_HDD_CATEGORY_ID = "56083"
_EBAY_UK_SITE_CURRENCY = "GBP"
_EBAY_UK_COUNTRY = "GB"
_MAX_TITLE_CHARS = 80
_MIN_ITEM_SPECIFICS_KEYS = 20
# Public — server.py imports these directly; single source of truth for the
# two eBay PictureDetails caps (24 URLs, 3975 joined chars).
MAX_PICTURE_URLS = 24
MAX_PICTURE_URLS_JOINED_CHARS = 3975

# Business Policies (issue #29) — eBay account is enrolled.
# Payment + Return profiles attach via SellerProfiles for ALL operations.
# Shipping is treated differently:
#   - AddItem (new listings): NO shipping policy attached — inline ShippingDetails
#     with FreeShipping=true preserves Simple Delivery's "Who pays?" defaulting
#     to seller-pays.
#   - ReviseFixedPriceItem (existing listings): shipping policy ref preserved
#     so revises don't accidentally detach existing listings from default-shipping.
_REQUIRED_SELLER_PROFILE_ENV_VARS = (
    "EBAY_PAYMENT_PROFILE_ID",
    "EBAY_SHIPPING_PROFILE_ID",
    "EBAY_RETURN_PROFILE_ID",
)


def _build_seller_profiles_block(include_shipping: bool = True) -> dict:
    """Return the SellerProfiles dict for AddItem/ReviseFixedPriceItem payloads.

    include_shipping: True for revise (preserves shipping policy ref on existing
        listings); False for add (new listings use inline ShippingDetails so
        Simple Delivery's "Who pays?" default stays seller-pays).

    Reads Profile IDs from env. Fails fast (RuntimeError) if any required
    var is missing. EBAY_SHIPPING_PROFILE_ID is required even when not used
    in AddItem so the env contract stays consistent across deployments.
    """
    missing = [k for k in _REQUIRED_SELLER_PROFILE_ENV_VARS if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Business Policies env vars not set: "
            f"{', '.join(missing)}. See .env.example and issue #29."
        )
    profiles: dict = {
        "SellerPaymentProfile": {
            "PaymentProfileID": os.environ["EBAY_PAYMENT_PROFILE_ID"],
        },
        "SellerReturnProfile": {
            "ReturnProfileID": os.environ["EBAY_RETURN_PROFILE_ID"],
        },
    }
    if include_shipping:
        profiles["SellerShippingProfile"] = {
            "ShippingProfileID": os.environ["EBAY_SHIPPING_PROFILE_ID"],
        }
    return profiles


def _parse_iso_ts(value: object) -> str | None:
    """Coerce ebaysdk timestamp (may be datetime / str) to ISO-8601 Z string.

    ebaysdk returns datetime objects whose `str()` yields naive formats like
    '2026-03-24 19:12:19'. eBay transmits timestamps in UTC — surface that
    explicitly by appending 'Z' when the input has no tz suffix.
    """
    if value is None or value == "":
        return None
    s = str(value)
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    if s.endswith("Z"):
        return s
    # Replace space separator (datetime str()) with 'T', then add Z for UTC.
    normalised = s.replace(" ", "T", 1)
    return normalised + "Z"


def _flatten_shipping_for_output(item: object) -> dict[str, object] | None:
    """Flattened shipping dict for analytics output (consumer-facing summary)."""
    sd = getattr(item, "ShippingDetails", None)
    if sd is None:
        return None
    sso = getattr(sd, "ShippingServiceOptions", None)
    if sso is None:
        return None
    # ebaysdk may return list or single; guard against empty list edge case
    if isinstance(sso, list):
        if not sso:
            return None
        first = sso[0]
    else:
        first = sso
    service = getattr(first, "ShippingService", None)
    cost_obj = getattr(first, "ShippingServiceCost", None)
    free = getattr(first, "FreeShipping", None)
    cost_val = None
    if cost_obj is not None:
        cost_val = getattr(cost_obj, "value", cost_obj)
    try:
        cost_float = float(cost_val) if cost_val is not None else None
    except (TypeError, ValueError):
        cost_float = None
    free_bool = str(free).lower() == "true" if free is not None else None
    out: dict[str, object] = {}
    if service is not None:
        out["service"] = str(service)
    if cost_float is not None:
        out["cost_gbp"] = cost_float
    if free_bool is not None:
        out["free"] = free_bool
    return out or None


def _flatten_return_policy_for_output(item: object) -> dict[str, object] | None:
    """Flattened return policy for analytics output."""
    rp = getattr(item, "ReturnPolicy", None)
    if rp is None:
        return None
    accepted_raw = getattr(rp, "ReturnsAcceptedOption", None)
    accepted = None
    if accepted_raw is not None:
        accepted = str(accepted_raw) == "ReturnsAccepted"
    period = getattr(rp, "ReturnsWithinOption", None)
    period_days = None
    if period is not None:
        ps = str(period)
        # e.g. "Days_30", "Days_14" — strip prefix
        if ps.startswith("Days_"):
            try:
                period_days = int(ps.split("_", 1)[1])
            except (ValueError, IndexError):
                period_days = None
    buyer_pays_raw = getattr(rp, "ShippingCostPaidByOption", None)
    buyer_pays = None
    if buyer_pays_raw is not None:
        buyer_pays = str(buyer_pays_raw) == "Buyer"
    out: dict[str, object] = {}
    if accepted is not None:
        out["returns_accepted"] = accepted
    if period_days is not None:
        out["period_days"] = period_days
    if buyer_pays is not None:
        out["buyer_pays"] = buyer_pays
    return out or None


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

    # Issue #4 Phase 1.1 — surface fields already fetched but previously dropped.
    selling_status = getattr(item, "SellingStatus", None)
    quantity_sold = 0
    if selling_status is not None:
        quantity_sold = int(getattr(selling_status, "QuantitySold", 0) or 0)

    listing_details = getattr(item, "ListingDetails", None)
    start_time = None
    end_time = None
    relist_count = 0
    promoted_listing = False
    best_offer_auto_accept_gbp: float | None = None
    best_offer_auto_decline_gbp: float | None = None
    if listing_details is not None:
        start_time = _parse_iso_ts(getattr(listing_details, "StartTime", None))
        end_time = _parse_iso_ts(getattr(listing_details, "EndTime", None))
        relist_count = int(getattr(listing_details, "RelistCount", 0) or 0)
        # Phase 1.4 — Auto-Ads safety net. Doc 14 forbids Promoted Listings,
        # but eBay's account-level Auto Ads rules can enrol listings without
        # per-listing opt-in. Surfacing this field lets the weekly sweep
        # detect accidental enrolment as a compliance breach.
        promoted_raw = getattr(listing_details, "PromotedListing", None)
        if promoted_raw is not None:
            promoted_listing = str(promoted_raw).lower() == "true"
        # AP #18 surfaced gap: ListingDetails carries BestOfferAutoAcceptPrice
        # + MinimumBestOfferPrice (the auto-decline floor) when Best Offer is
        # configured. Required for safe restore/revise round-trips and for
        # recommend_best_offer_thresholds to compare current vs proposed.
        accept_amt = getattr(listing_details, "BestOfferAutoAcceptPrice", None)
        decline_amt = getattr(listing_details, "MinimumBestOfferPrice", None)
        if accept_amt is not None:
            try:
                best_offer_auto_accept_gbp = float(getattr(accept_amt, "value", accept_amt))
            except (TypeError, ValueError):
                best_offer_auto_accept_gbp = None
        if decline_amt is not None:
            try:
                best_offer_auto_decline_gbp = float(getattr(decline_amt, "value", decline_amt))
            except (TypeError, ValueError):
                best_offer_auto_decline_gbp = None

    days_on_site = None
    if start_time is not None:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            days_on_site = max(0, (datetime.now(timezone.utc) - start_dt).days)
        except (ValueError, TypeError):
            days_on_site = None

    best_offer_count = int(getattr(item, "BestOfferCount", 0) or 0)
    question_count = int(getattr(item, "QuestionCount", 0) or 0)
    # Boolean-only contract (#16 fix per Stage 1 L2 F1): default-to-"false" when
    # the BestOfferEnabled element is absent (eBay omits it for listings that
    # don't have Best Offer configured). Field is nested under BestOfferDetails
    # — reading directly off the Item element always returned absent, masking
    # any listing where Best Offer was enabled and producing 0/N false-negatives
    # (caught 2026-05-02 during ebay-ops#17 audit gap).
    best_offer_details = getattr(item, "BestOfferDetails", None)
    best_offer_enabled_raw = (
        getattr(best_offer_details, "BestOfferEnabled", "false")
        if best_offer_details is not None
        else "false"
    )
    best_offer_enabled = str(best_offer_enabled_raw).lower() == "true"

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
        "quantity_sold": quantity_sold,
        "watch_count": int(getattr(item, "WatchCount", 0) or 0),
        # HitCount is deprecated by eBay:
        # https://developer.ebay.com/devzone/xml/docs/reference/ebay/GetItem.html
        # "Hit counters are no longer shown in View Item pages, so this field is
        # no longer applicable." Always 0 / absent on modern responses.
        # view_count is populated by analyse_listing's Phase 2 enrichment using
        # the Analytics API LISTING_VIEWS_TOTAL metric. Here we always return
        # None to preserve the data-gap signal end-to-end.
        "view_count": None,
        "best_offer_count": best_offer_count,
        "best_offer_enabled": best_offer_enabled,
        "best_offer_auto_accept_gbp": best_offer_auto_accept_gbp,
        "best_offer_auto_decline_gbp": best_offer_auto_decline_gbp,
        "question_count": question_count,
        "relist_count": relist_count,
        "promoted_listing": promoted_listing,
        "start_time": start_time,
        "end_time": end_time,
        "days_on_site": days_on_site,
        "shipping": _flatten_shipping_for_output(item),
        "return_policy": _flatten_return_policy_for_output(item),
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
        "condition_id": d["condition_id"],
        "condition_name": d["condition_name"],
        "description_length": d["description_length"],
        "description_hash": hashlib.sha256(d["description_html"].encode()).hexdigest()[:16],
        "quantity": d["quantity"],
    }


def compute_diff(
    before: dict,
    title: str | None,
    description_html: str | None,
    price: float | None,
    condition_id: int | None = None,
    condition_description: str | None = None,
    item_specifics: dict[str, str | list[str]] | None = None,
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
    if condition_id is not None and str(condition_id) != before.get("condition_id"):
        diff["condition_id"] = {
            "before": before.get("condition_id"),
            "after": str(condition_id),
        }
    if condition_description is not None:
        diff["condition_description"] = {"after": condition_description}
    if item_specifics is not None:
        diff["item_specifics"] = {"after_count": len(item_specifics)}
    return diff


def cdata_wrap(html: str) -> str:
    """Wrap HTML in CDATA for eBay XML payload. Handles ]]> in content."""
    # eBay XML-escapes HTML unless it's in CDATA. Handle the edge case where
    # the HTML itself contains ]]> by splitting and re-opening CDATA sections.
    escaped = html.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{escaped}]]>"


def _decimal_str(value: float | int | str) -> str:
    """Two-dp string for an Amount value, via Decimal(str(...)) to avoid float drift."""
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def build_revise_payload(
    item_id: str,
    title: str | None = None,
    description_html: str | None = None,
    price: float | None = None,
    condition_id: int | None = None,
    condition_description: str | None = None,
    item_specifics: dict[str, str | list[str]] | None = None,
    picture_urls: list[str] | None = None,
    best_offer_enabled: bool | None = None,
    best_offer_auto_accept_gbp: float | None = None,
    best_offer_auto_decline_gbp: float | None = None,
    currency: str = _EBAY_UK_SITE_CURRENCY,
) -> dict:
    """Build the ReviseFixedPriceItem payload dict.

    NEVER includes a Quantity key at any nesting level — this is a safety
    invariant verified by whitebox test.

    NOTE (Issue #14 AC1.5): the ``price`` arg is intentionally
    ``update_listing``-only. ``revise_listing_pictures`` (the only other
    in-tree caller, at ``ebay/pictures.py``) relies on the ``None`` default
    and never sets a price; the kwarg-default IS the structural guard. Do
    not promote ``price`` to a positional parameter or change the default
    without auditing every caller — a stray non-None price here would
    revise the listing's price as a side-effect of a picture-only update.

    Shipping/payment/returns are emitted via SellerProfiles (Business Policies,
    issue #29). The previous ``shipping_details`` parameter and inline
    ShippingDetails default were removed when the account enrolled — eBay
    rejects mixed-mode payloads.

    condition_id: eBay condition ID (1000=New, 1500=Opened-never used,
    3000=Used, 7000=For parts or not working).

    condition_description: Free-text seller notes for eBay "Seller notes" field.

    item_specifics: Dict of name -> value(s). Single string or list of strings.
    Replaces the entire ItemSpecifics block on the listing.

    picture_urls: Replace the listing's PictureDetails.PictureURL list. eBay
    accepts up to MAX_PICTURE_URLS (24); the caller is responsible for
    composing append-vs-replace semantics — this function only writes the
    final ordered URL list.

    best_offer_enabled: When True/False, sets Item.BestOfferDetails.BestOfferEnabled.
    None leaves the listing's current Best Offer toggle untouched.

    best_offer_auto_accept_gbp / best_offer_auto_decline_gbp: Trading API field
    placement (D2 verified): Item.ListingDetails.BestOfferAutoAcceptPrice and
    Item.ListingDetails.MinimumBestOfferPrice (NOT under BestOfferDetails).
    Decimal-stringified to two dp; currency echoes location_details["Currency"].
    """
    item: dict = {"ItemID": item_id}
    if title is not None:
        item["Title"] = title
    if description_html is not None:
        item["Description"] = cdata_wrap(description_html)
    if price is not None:
        item["StartPrice"] = str(price)
    if condition_id is not None:
        item["ConditionID"] = str(condition_id)
    if condition_description is not None:
        item["ConditionDescription"] = condition_description
    if item_specifics is not None:
        nvl = []
        for name, value in item_specifics.items():
            if isinstance(value, list):
                nvl.append({"Name": name, "Value": value})
            else:
                nvl.append({"Name": name, "Value": [value]})
        item["ItemSpecifics"] = {"NameValueList": nvl}

    if picture_urls is not None:
        if len(picture_urls) > MAX_PICTURE_URLS:
            raise ValueError(
                f"picture_urls must contain at most {MAX_PICTURE_URLS} URLs "
                f"(got {len(picture_urls)})"
            )
        joined_urls_len = sum(len(u) for u in picture_urls)
        if joined_urls_len >= MAX_PICTURE_URLS_JOINED_CHARS:
            raise ValueError(
                f"picture_urls total length {joined_urls_len} chars exceeds eBay "
                f"<{MAX_PICTURE_URLS_JOINED_CHARS} cap"
            )
        item["PictureDetails"] = {"PictureURL": list(picture_urls)}

    if best_offer_enabled is not None:
        item["BestOfferDetails"] = {"BestOfferEnabled": "true" if best_offer_enabled else "false"}

    if best_offer_auto_accept_gbp is not None or best_offer_auto_decline_gbp is not None:
        listing_details = item.setdefault("ListingDetails", {})
        # ebaysdk 2.2.0 dict→XML serialiser: `{"#text": V, "@attrs": {"currencyID": C}}`
        # emits `<X currencyID="C">V</X>`. The legacy `{"value": V, "_currencyID": C}`
        # form serialises as nested children and triggers eBay schema error 20170.
        if best_offer_auto_accept_gbp is not None:
            listing_details["BestOfferAutoAcceptPrice"] = {
                "#text": _decimal_str(best_offer_auto_accept_gbp),
                "@attrs": {"currencyID": currency},
            }
        if best_offer_auto_decline_gbp is not None:
            listing_details["MinimumBestOfferPrice"] = {
                "#text": _decimal_str(best_offer_auto_decline_gbp),
                "@attrs": {"currencyID": currency},
            }

    # Business Policies (issue #29): SellerProfiles supplies shipping + payment
    # + returns via account-level Profile IDs. Inline ShippingDetails removed.
    item["SellerProfiles"] = _build_seller_profiles_block()

    payload = {"Item": item}

    # Whitebox safety: verify no Quantity key leaked in
    _assert_no_quantity(payload)

    return payload


def build_add_payload(
    title: str,
    description_html: str,
    price: float,
    quantity: int,
    condition_id: int,
    condition_description: str | None,
    item_specifics: dict[str, str | list[str]],
    picture_urls: list[str],
    uuid_hex: str,
    location_details: dict | None = None,
) -> dict:
    """Build the AddFixedPriceItem payload dict.

    Add-path counterpart to build_revise_payload. Enforces the Add invariant
    (_assert_requires_quantity) — the Revise invariant (_assert_no_quantity)
    is intentionally NOT called here because Add MUST carry Quantity.

    UUID (32-char uppercase hex) is emitted as Item.UUID for idempotent retry —
    eBay returns DuplicateInvocationDetails on replay instead of creating
    a second listing.

    Shipping/payment/returns are emitted via SellerProfiles (Business Policies,
    issue #29). The previous ``shipping_details`` and ``return_policy``
    parameters were removed when the account enrolled — eBay rejects
    mixed-mode payloads on enrolled accounts.

    location_details: default reads EBAY_SELLER_LOCATION + EBAY_SELLER_POSTCODE
    from the environment eagerly. auth.py validates these at startup, so an
    unset env here is a programmer error and raises KeyError fast.
    """
    if not _UUID_RE.match(uuid_hex):
        raise ValueError(
            f"uuid_hex must match ^[0-9A-F]{{32}}$ (32 upper-hex chars); got {uuid_hex!r}"
        )
    if len(title) > _MAX_TITLE_CHARS:
        raise ValueError(f"title exceeds {_MAX_TITLE_CHARS}-char eBay limit (got {len(title)})")
    if not picture_urls:
        raise ValueError("picture_urls must contain at least 1 URL")
    if len(picture_urls) > MAX_PICTURE_URLS:
        raise ValueError(
            f"picture_urls must contain at most {MAX_PICTURE_URLS} URLs (got {len(picture_urls)})"
        )
    joined_urls_len = sum(len(u) for u in picture_urls)
    if joined_urls_len >= MAX_PICTURE_URLS_JOINED_CHARS:
        raise ValueError(
            f"picture_urls total length {joined_urls_len} chars exceeds eBay "
            f"<{MAX_PICTURE_URLS_JOINED_CHARS} cap"
        )
    # Category 56083 mandates Brand + MPN; the canonical 21-field table also
    # sets a ≥20-key floor (some sellers omit Colour / Country of Origin,
    # but our listings enforce the full set).
    if "Brand" not in item_specifics:
        raise ValueError(
            "item_specifics missing required field 'Brand' (category 56083 rejects payload)"
        )
    if "MPN" not in item_specifics:
        raise ValueError(
            "item_specifics missing required field 'MPN' (category 56083 rejects payload)"
        )
    if len(item_specifics) < _MIN_ITEM_SPECIFICS_KEYS:
        raise ValueError(
            f"item_specifics must have at least {_MIN_ITEM_SPECIFICS_KEYS} keys "
            f"(got {len(item_specifics)}) — see research §1.3 canonical 21-field table"
        )

    if location_details is None:
        location_details = {
            "Country": _EBAY_UK_COUNTRY,
            "Location": os.environ["EBAY_SELLER_LOCATION"],
            "PostalCode": os.environ["EBAY_SELLER_POSTCODE"],
            "Currency": _EBAY_UK_SITE_CURRENCY,
        }
    # AddItem path: NO shipping policy reference; inline ShippingDetails below.
    seller_profiles = _build_seller_profiles_block(include_shipping=False)

    # Inline ShippingDetails (FreeShipping=true) so Simple Delivery's
    # "Who pays?" defaults to seller-pays on new listings. NO shipping policy
    # reference — Simple Delivery is the operative shipping layer.
    shipping_details = {
        "ShippingType": "Flat",
        "GlobalShipping": "true",
        "ShippingServiceOptions": {
            "ShippingServicePriority": "1",
            "ShippingService": "UK_RoyalMailSecondClassStandard",
            "ShippingServiceCost": {
                "#text": "0.00",
                "@attrs": {"currencyID": location_details["Currency"]},
            },
            "FreeShipping": "true",
        },
    }

    nvl = []
    for name, value in item_specifics.items():
        if isinstance(value, list):
            nvl.append({"Name": name, "Value": value})
        else:
            nvl.append({"Name": name, "Value": [value]})

    item: dict = {
        "Title": title,
        "Description": cdata_wrap(description_html),
        "PrimaryCategory": {"CategoryID": _HDD_CATEGORY_ID},
        "StartPrice": {
            "#text": f"{price:.2f}",
            "@attrs": {"currencyID": location_details["Currency"]},
        },
        "Quantity": str(int(quantity)),
        "ConditionID": str(condition_id),
        "ListingType": "FixedPriceItem",
        "ListingDuration": "GTC",
        "Currency": location_details["Currency"],
        "Country": location_details["Country"],
        "Location": location_details["Location"],
        "PostalCode": location_details["PostalCode"],
        "Site": "UK",
        "DispatchTimeMax": "3",
        "UUID": uuid_hex,
        "PictureDetails": {"PictureURL": list(picture_urls)},
        "ItemSpecifics": {"NameValueList": nvl},
        "ShippingDetails": shipping_details,
        "SellerProfiles": seller_profiles,
    }
    if condition_description is not None:
        item["ConditionDescription"] = condition_description

    payload = {"Item": item}
    _assert_requires_quantity(payload)
    return payload


def _assert_no_quantity(d: dict | list, path: str = "") -> None:
    """Recursively verify no Quantity key exists in the payload.

    Revise-path invariant. Quantity on eBay is reserved as the single source
    of truth for stock count — tool MUST NOT overwrite it via ReviseFixedPriceItem.
    """
    if isinstance(d, dict):
        for k, v in d.items():
            if k.lower() == "quantity":
                raise ValueError(f"SAFETY: Quantity key found at {path}.{k} — refusing to build")
            _assert_no_quantity(v, f"{path}.{k}")
    elif isinstance(d, list):
        for i, v in enumerate(d):
            _assert_no_quantity(v, f"{path}[{i}]")


def _assert_requires_quantity(payload: dict, min: int = 1) -> None:
    """Add-path invariant: Quantity MUST be present and >= min.

    Opposite of _assert_no_quantity. AddFixedPriceItem refuses to create a
    listing without an initial Quantity; this guard fails loudly at build time
    rather than round-tripping to eBay with a malformed payload.
    """
    item = payload.get("Item", {})
    q = item.get("Quantity")
    if q is None:
        raise ValueError("SAFETY: Add payload missing Quantity — refusing to build")
    try:
        qv = int(q)
    except (TypeError, ValueError) as e:
        raise ValueError(f"SAFETY: Add Quantity={q!r} not int-coercible — refusing") from e
    if qv < min:
        raise ValueError(f"SAFETY: Add Quantity={qv} < min={min} — refusing")


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
    condition_before: str | None = None,
    condition_after: str | None = None,
) -> None:
    """Append one JSON line to the audit log.

    Disk-full: catches OSError, logs to stderr via log_debug. The eBay update
    is NOT rolled back for a log failure — but the caller is informed via
    the log_debug warning.
    """
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "item_id": item_id,
        "fields_changed": fields_changed,
        "before_length": before_length,
        "after_length": after_length,
        "success": success,
        "error": error,
        "local_html_path": local_html_path,
    }
    if condition_before or condition_after:
        entry["condition_before"] = condition_before
        entry["condition_after"] = condition_after
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
