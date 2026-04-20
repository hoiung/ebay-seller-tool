"""
ebay-seller-tool MCP server.

Provides tools for managing eBay listings from Claude Code.
Uses eBay Trading API (XML) for listing CRUD and photo uploads.
"""

import asyncio
import glob
import json
import logging
import os
import re
import traceback
import uuid
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE importing ebay modules that read env vars
load_dotenv()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from ebay.auth import check_token_expiry, validate_credentials  # noqa: E402
from ebay.client import execute_with_retry, log_debug  # noqa: E402
from ebay.hdd_specs import HDD_SPECS  # noqa: E402
from ebay.listings import (  # noqa: E402
    audit_log_write,
    build_add_payload,
    build_revise_payload,
    compute_diff,
    extract_shipping_details,
    listing_to_dict,
    snapshot_listing,
)
from ebay.photos import preprocess_for_ebay, upload_one  # noqa: E402

MAX_PHOTOS_PER_LISTING = 24
MAX_PICTURE_URLS_JOINED_CHARS = 3975
UPLOAD_RATE_LIMIT_SLEEP_SECONDS = 0.5

# Condition string → eBay ConditionID (category 56083, research §1.5).
CONDITION_MAP: dict[str, int] = {
    "New": 1000,
    "Opened": 1500,
    "Used": 3000,
    "Used - Excellent": 2750,
}

# File-suffix mapping for listing-<suffix>.html resolution (P3.3).
CONDITION_HTML_SUFFIX: dict[str, str] = {
    "New": "new",
    "Opened": "open-box",
    "Used": "used",
    "Used - Excellent": "used-excellent",
}

# eBay image filenames from Hoi's phone camera (timestamp pattern).
LABEL_PHOTO_REGEX = re.compile(r"IMG\d{8}\d{6}\.jpg$", re.IGNORECASE)
_COPY_BLOCK_RE = re.compile(
    r'<div[^>]*class=["\'][^"\']*copy-block[^"\']*["\'][^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# UUID cache keyed by folder_path — stabilises retries within a process lifetime
# so the second invocation for the same folder re-uses the UUID and eBay replies
# with DuplicateInvocationDetails (P3.7, P3.11).
_create_listing_uuid_cache: dict[str, str] = {}


def _derive_transfer_rate(title: str) -> str:
    """Transfer Rate derivation (title-authoritative per P3.5)."""
    t = title.lower()
    if "12gb/s" in t or "sas-3" in t:
        return "12G"
    if "3gb/s" in t or "sata ii" in t:
        return "3G"
    # SATA III, SATA 6Gb/s, SAS 6Gb/s — all 6G
    return "6G"


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


def _extract_title_from_html(html: str) -> str | None:
    """Title source order: copy-block 'Title: ...' row → <h1> text → None."""
    cb = _COPY_BLOCK_RE.search(html)
    if cb:
        inner = _strip_html(cb.group(1))
        for line in (ln.strip() for ln in inner.splitlines()):
            if not line:
                continue
            if line.lower().startswith("title:"):
                return line.split(":", 1)[1].strip()
        # No "Title:" row — treat the first non-empty line as the title
        first = next((ln.strip() for ln in inner.splitlines() if ln.strip()), None)
        if first:
            return first
    h1 = _H1_RE.search(html)
    if h1:
        return _strip_html(h1.group(1))
    return None


def _resolve_description_html(
    folder: Path,
    condition: str,
    description_html_override: str | None,
) -> str:
    if description_html_override is not None:
        return description_html_override
    suffix = CONDITION_HTML_SUFFIX[condition]
    variant = folder / f"listing-{suffix}.html"
    if variant.exists():
        return variant.read_text(encoding="utf-8")
    single = folder / "listing.html"
    if single.exists():
        return single.read_text(encoding="utf-8")
    # Fall back to Jinja template — caller hasn't authored HTML yet.
    template_path = Path(__file__).parent / "templates" / "listing_description.html"
    if not template_path.exists():
        raise FileNotFoundError(
            f"no listing HTML in {folder} and no template at {template_path}"
        )
    # Late import keeps startup cost down on the common path.
    from jinja2 import Template  # noqa: PLC0415

    rendered = Template(template_path.read_text(encoding="utf-8")).render(
        folder=folder.name,
        condition=condition,
    )
    return rendered


def _extract_oem_model(folder_path: str) -> str:
    """The folder basename IS the OEM model — this is Hoi's established convention."""
    return Path(folder_path).name


def _build_21_field_specifics(
    oem_model: str,
    title: str,
    has_caddy: bool,
    specs: dict[str, str | None],
) -> dict[str, str | list[str]]:
    """Canonical 21-field ItemSpecifics per research §1.3."""
    storage_format = "HDD with Caddy" if has_caddy else "HDD Only"
    specifics: dict[str, str | list[str]] = {
        "Brand": specs["brand"] or "",
        "MPN": oem_model,
        "Model": oem_model,
        "Product Line": specs["family"] or "",
        "Type": "Internal Hard Drive",
        "Drive Type(s) Supported": "HDD",
        "Storage Format": storage_format,
        "Storage Capacity": specs["capacity"] or "",
        "Interface": specs["interface"] or "",
        "Form Factor": specs["form_factor"] or "",
        "Rotation Speed": specs["rpm"] or "",
        "Cache": specs["cache"] or "",
        "Transfer Rate": _derive_transfer_rate(title),
        "Compatible With": "PC",
        "Features": ["Hot Swap", "24/7 Operation"],
        "Colour": "Silver",
        "Country of Origin": "China",
        "EAN": "Does not apply",
        "Manufacturer Warranty": "See Item Description",
        "Unit Type": "Unit",
    }
    # Height applies to 2.5" drives only (3.5" has no 15mm/9.5mm variant).
    if specs.get("height"):
        specifics["Height"] = specs["height"] or ""
    return specifics


def _glob_label_photos(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    candidates = sorted(str(p) for p in folder.glob("IMG*.jpg"))
    return [p for p in candidates if LABEL_PHOTO_REGEX.search(Path(p).name)]

mcp = FastMCP("ebay-seller-tool")


# Suppress ebaysdk logging unless EBAY_DEBUG=1
if not os.environ.get("EBAY_DEBUG"):
    logging.getLogger("ebaysdk").setLevel(logging.CRITICAL)

# Validate credentials at module load (runs whether invoked via __main__ or MCP framework)
validate_credentials()
check_token_expiry()


def with_error_handling(func):
    """Decorator for consistent error reporting across all MCP tools."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except SystemExit:
            raise
        except Exception as e:
            error_details = traceback.format_exc()
            log_debug(f"ERROR in {func.__name__}: {error_details}")
            return json.dumps(
                {"error": str(e), "tool": func.__name__, "details": error_details},
                indent=2,
            )

    return wrapper


@mcp.tool()
@with_error_handling
async def get_active_listings(page: int = 1, per_page: int = 25) -> str:
    """Get all active eBay listings with title, price, quantity, and watchers.

    Args:
        page: Page number (1-based). Default 1.
        per_page: Listings per page (1-200). Default 25.

    Returns:
        JSON with total count, page info, and listing details.
    """
    log_debug(f"get_active_listings page={page} per_page={per_page}")

    if page < 1:
        return json.dumps({"error": "page must be >= 1"})
    if per_page < 1 or per_page > 200:
        return json.dumps({"error": "per_page must be between 1 and 200"})

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetMyeBaySelling",
        {
            "ActiveList": {
                "Sort": "TimeLeft",
                "Pagination": {
                    "EntriesPerPage": per_page,
                    "PageNumber": page,
                },
            },
        },
    )

    if not hasattr(response.reply, "ActiveList") or response.reply.ActiveList is None:
        log_debug("get_active_listings result total=0 reason=no_active_list")
        return json.dumps({"total": 0, "page": page, "per_page": per_page, "listings": []})

    active_list = response.reply.ActiveList

    # Read the real store total from PaginationResult — even on out-of-bounds
    # pages this is set, so we don't lie with total=0 when the store actually
    # has listings on earlier pages.
    real_total = 0
    if hasattr(active_list, "PaginationResult") and active_list.PaginationResult is not None:
        try:
            real_total = int(active_list.PaginationResult.TotalNumberOfEntries)
        except (AttributeError, ValueError, TypeError):
            real_total = 0

    # Handle zero listings (ItemArray absent or empty, or Item absent).
    # This also handles out-of-bounds pages — total reflects the real store size.
    if (
        not hasattr(active_list, "ItemArray")
        or active_list.ItemArray is None
        or not hasattr(active_list.ItemArray, "Item")
        or active_list.ItemArray.Item is None
    ):
        log_debug(f"get_active_listings result total={real_total} returned=0")
        return json.dumps({"total": real_total, "page": page, "per_page": per_page, "listings": []})

    items = active_list.ItemArray.Item

    # Handle single-item response (ebaysdk returns dict not list for 1 item)
    if not isinstance(items, list):
        items = [items]

    total = real_total

    listings = []
    for item in items:
        selling_status = item.SellingStatus
        listing = {
            "item_id": str(item.ItemID),
            "title": str(item.Title),
            "price": {
                "amount": str(selling_status.CurrentPrice.value),
                "currency": str(selling_status.CurrentPrice._currencyID),
            },
            "quantity_available": int(item.QuantityAvailable),
            "watch_count": int(getattr(item, "WatchCount", 0) or 0),
            "view_count": int(getattr(item, "HitCount", 0) or 0),
            "listing_url": str(item.ListingDetails.ViewItemURL),
        }
        listings.append(listing)

    log_debug(f"get_active_listings result total={total} returned={len(listings)}")

    return json.dumps(
        {
            "total": total,
            "page": page,
            "per_page": per_page,
            "listings": listings,
        },
        indent=2,
    )


@mcp.tool()
@with_error_handling
async def get_listing_details(item_id: str) -> str:
    """Get full details for a single eBay listing.

    Returns description HTML, item specifics, photos, and all metadata.

    Args:
        item_id: The eBay item ID (numeric string).

    Returns:
        JSON with full listing details or error.
    """
    if not item_id or not item_id.strip():
        return json.dumps({"error": "item_id required"})

    log_debug(f"get_listing_details item_id={item_id}")

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll", "IncludeItemSpecifics": "true"},
    )

    if response.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})

    result = listing_to_dict(response.reply.Item)
    log_debug(
        f"get_listing_details OK item_id={item_id} title={result['title'][:50]} "
        f"desc_len={result['description_length']}"
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def update_listing(
    item_id: str,
    title: str | None = None,
    description_html: str | None = None,
    price: float | None = None,
    condition_id: int | None = None,
    condition_description: str | None = None,
    item_specifics: dict | None = None,
    dry_run: bool = False,
) -> str:
    """Update any listing field except quantity on an existing listing.

    Supports: title, description, price, condition, condition description,
    and item specifics. Quantity is intentionally blocked.

    Args:
        item_id: The eBay item ID to update.
        title: New title (max 80 chars). Optional.
        description_html: New description HTML. Optional.
        price: New price (must be > 0). Optional.
        condition_id: eBay condition ID (1000=New, 1500=Opened-never used, 3000=Used). Optional.
        condition_description: Seller notes text for condition. Optional.
        item_specifics: Dict of name->value(s) for item specifics. Optional.
        dry_run: If True, return diff without making changes. Default False.

    Returns:
        JSON with diff (dry_run) or success result with before/after snapshots.
    """
    if not item_id or not item_id.strip():
        return json.dumps({"error": "item_id required"})

    updatable = [
        title, description_html, price,
        condition_id, condition_description, item_specifics,
    ]
    has_update = any(v is not None for v in updatable)
    if not has_update:
        return json.dumps({"error": "no fields to update"})

    if title is not None and len(title) > 80:
        return json.dumps({"error": f"title exceeds 80-char eBay limit (got {len(title)})"})
    if price is not None and price <= 0:
        return json.dumps({"error": "price must be > 0"})
    if condition_id is not None and condition_id not in (1000, 1500, 3000):
        return json.dumps({
            "error": f"invalid condition_id {condition_id}. Valid: 1000=New, "
            "1500=Opened-never used, 3000=Used. "
            "7000 (For parts) is blocked — use eBay Seller Hub directly.",
        })
    if description_html is not None:
        description_html = description_html.strip()
        if not description_html:
            return json.dumps({"error": "description_html must not be empty"})
        if "<" not in description_html or ">" not in description_html:
            return json.dumps({"error": "description_html must contain at least one HTML tag"})

    update_fields = [
        f for f in [
            "title", "description_html", "price",
            "condition_id", "condition_description", "item_specifics",
        ]
        if locals().get(f) is not None
    ]
    log_debug(f"update_listing item_id={item_id} dry_run={dry_run} fields={update_fields}")

    # Fetch current state for diff
    current = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll", "IncludeItemSpecifics": "true"},
    )
    if current.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})

    # Single listing_to_dict call — used for snapshot, diff, AND specifics merge
    current_full = listing_to_dict(current.reply.Item)
    before = snapshot_listing(current.reply.Item)

    diff = compute_diff(
        before, title, description_html, price,
        condition_id, condition_description, item_specifics,
    )

    log_debug(
        f"DIFF item_id={item_id} fields_to_change={list(diff.keys())} "
        f"before_len={before['description_length']} "
        f"after_len={len(description_html) if description_html else before['description_length']}"
    )

    if not diff:
        return json.dumps(
            {
                "item_id": item_id,
                "no_change": True,
                "message": "all fields identical",
            }
        )

    if dry_run:
        return json.dumps({"dry_run": True, "item_id": item_id, "diff": diff}, indent=2)

    # Build and send ReviseFixedPriceItem payload — echo back current shipping config
    shipping = extract_shipping_details(current.reply.Item)

    # Item specifics: eBay replaces the entire block, so merge new values into existing
    merged_specifics = None
    if item_specifics is not None:
        merged_specifics = dict(current_full.get("specifics", {}))
        for k, v in item_specifics.items():
            merged_specifics[k] = v if isinstance(v, list) else [v]

    payload = build_revise_payload(
        item_id, title, description_html, price, shipping,
        condition_id, condition_description, merged_specifics,
    )
    await asyncio.to_thread(execute_with_retry, "ReviseFixedPriceItem", payload)

    # Verify by re-fetching
    after_resp = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {"ItemID": item_id, "DetailLevel": "ReturnAll", "IncludeItemSpecifics": "true"},
    )
    if after_resp.reply.Item is None:
        log_debug(f"update_listing VERIFY_FAILED item_id={item_id} — item disappeared after update")
        audit_log_write(
            item_id=item_id,
            fields_changed=list(diff.keys()),
            before_length=before["description_length"],
            after_length=0,
            success=True,
            error="update applied but post-verify fetch returned empty item",
        )
        return json.dumps(
            {
                "success": True,
                "item_id": item_id,
                "fields_updated": list(diff.keys()),
                "warning": "update applied but post-verify fetch returned empty item",
            },
            indent=2,
        )
    after = snapshot_listing(after_resp.reply.Item)

    # Audit log
    audit_log_write(
        item_id=item_id,
        fields_changed=list(diff.keys()),
        before_length=before["description_length"],
        after_length=after["description_length"],
        success=True,
        condition_before=before.get("condition_id"),
        condition_after=after.get("condition_id"),
    )

    log_debug(
        f"update_listing OK item_id={item_id} fields_updated={list(diff.keys())} "
        f"condition={before.get('condition_id')}->{after.get('condition_id')}"
    )

    return json.dumps(
        {
            "success": True,
            "item_id": item_id,
            "fields_updated": list(diff.keys()),
            "before": before,
            "after": after,
        },
        indent=2,
    )


@mcp.tool()
@with_error_handling
async def upload_photos(photo_paths: list[str], dry_run: bool = False) -> str:
    """Upload a list of local images to eBay Picture Services.

    Each image is preprocessed (EXIF-transposed, RGB, ≤ 1600×1600 JPEG q90,
    EXIF stripped) and uploaded ONE-PER-CALL because UploadSiteHostedPictures
    accepts only one picture per request (eBay KB 1063). Photo ordering is
    preserved end-to-end — photo_paths[0] ends up at PictureURL[0] which
    becomes the listing gallery image.

    Args:
        photo_paths: Ordered list of local image paths. 1..24 paths.
        dry_run: If True, skip the upload and return a preview of what would
            happen (projected output sizes, per-path valid/rejected).

    Returns:
        JSON. On success: {success, urls, total_url_chars, warnings}.
        On partial failure: {success: false, urls_uploaded_so_far,
        failed_at_index, error}. On invalid input: {error}.
    """
    if not photo_paths:
        return json.dumps({"error": "photo_paths must contain at least 1 path"})
    if len(photo_paths) > MAX_PHOTOS_PER_LISTING:
        return json.dumps({
            "error": (
                f"photo_paths exceeds eBay {MAX_PHOTOS_PER_LISTING}-image cap "
                f"(got {len(photo_paths)})"
            )
        })

    log_debug(f"upload_photos count={len(photo_paths)} dry_run={dry_run}")

    if dry_run:
        preview = []
        for p in photo_paths:
            try:
                bytes_out = await asyncio.to_thread(preprocess_for_ebay, p)
                preview.append({
                    "path": p,
                    "size_bytes_after_preprocess": len(bytes_out),
                    "rejected": False,
                })
            except ValueError as e:
                preview.append({"path": p, "rejected": True, "reason": str(e)})
        return json.dumps({
            "dry_run": True,
            "would_upload": len([p for p in preview if not p["rejected"]]),
            "preview": preview,
        }, indent=2)

    urls: list[str] = []
    for idx, p in enumerate(photo_paths):
        try:
            bytes_out = await asyncio.to_thread(preprocess_for_ebay, p)
            url = await asyncio.to_thread(upload_one, bytes_out)
            urls.append(url)
            if idx < len(photo_paths) - 1:
                await asyncio.sleep(UPLOAD_RATE_LIMIT_SLEEP_SECONDS)
        except Exception as e:
            log_debug(f"upload_photos FAILED at index={idx} error={e!r}")
            return json.dumps({
                "success": False,
                "urls_uploaded_so_far": urls,
                "failed_at_index": idx,
                "failed_path": p,
                "error": str(e),
            }, indent=2)

    total_chars = sum(len(u) for u in urls)
    warnings: list[str] = []
    if total_chars >= MAX_PICTURE_URLS_JOINED_CHARS:
        warnings.append(
            f"total_url_chars={total_chars} exceeds eBay soft cap "
            f"{MAX_PICTURE_URLS_JOINED_CHARS} — listing may reject PictureDetails"
        )

    return json.dumps({
        "success": True,
        "urls": urls,
        "total_url_chars": total_chars,
        "warnings": warnings,
    }, indent=2)


@mcp.tool()
@with_error_handling
async def create_listing(
    folder_path: str,
    price: float,
    quantity: int,
    condition: str,
    has_caddy: bool,
    photo_paths: list[str] | None = None,
    description_html: str | None = None,
    dry_run: bool = True,
    picture_urls: list[str] | None = None,
) -> str:
    """Create an eBay UK fixed-price listing end-to-end from a product folder.

    Defaults to dry_run=True (Verify API). Switch to dry_run=False with
    explicit caller intent. UUID caching stabilises retries within the
    process lifetime — a second invocation for the same folder re-uses
    the UUID and eBay returns DuplicateInvocationDetails rather than a
    duplicate listing.

    Args:
        folder_path: Product folder on Google Drive. Basename IS the OEM model
            (keyed into HDD_SPECS). `-EXOS` suffix valid.
        price: Listing price in GBP, > 0.
        quantity: Initial stock count, >= 1.
        condition: One of {New, Opened, Used, Used - Excellent}.
        has_caddy: True → Storage Format = "HDD with Caddy"; else "HDD Only".
        photo_paths: Ordered list of photo paths. If None, glob IMG*.jpg from
            folder.
        description_html: Override the HTML file in the folder. If None, the
            tool resolves listing-<suffix>.html → listing.html → Jinja template.
        dry_run: If True, call VerifyAddFixedPriceItem (default). If False,
            call AddFixedPriceItem for real.
        picture_urls: Skip upload and use these URLs. If None, tool calls
            upload_photos internally.

    Returns:
        JSON — see module docstring for full return shape contract.
    """
    # --- P3.2 input validation ---
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        return json.dumps({"error": f"folder_path not a directory: {folder_path}"})
    if price <= 0:
        return json.dumps({"error": f"price must be > 0 (got {price})"})
    if quantity < 1:
        return json.dumps({"error": f"quantity must be >= 1 (got {quantity})"})
    if condition not in CONDITION_MAP:
        return json.dumps({
            "error": f"invalid condition {condition!r}. "
            f"Valid: {sorted(CONDITION_MAP.keys())}"
        })
    if not isinstance(has_caddy, bool):
        return json.dumps({"error": "has_caddy must be bool"})
    if photo_paths is not None and not photo_paths:
        return json.dumps({"error": "photo_paths provided but empty"})

    condition_id = CONDITION_MAP[condition]
    oem_model = _extract_oem_model(folder_path)

    # --- P3.4 HDD_SPECS lookup (fail loud on unknown MPN) ---
    if oem_model not in HDD_SPECS:
        return json.dumps({
            "error": f"Unknown MPN {oem_model}. "
            "Add to ebay/hdd_specs.py before creating listing."
        })
    specs = HDD_SPECS[oem_model]

    # --- P3.3 HTML + title resolution ---
    try:
        resolved_html = _resolve_description_html(folder, condition, description_html)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})

    title = _extract_title_from_html(resolved_html)
    if title is None:
        return json.dumps({
            "error": "could not derive title from HTML (no copy-block, no <h1>); "
            "provide description_html with a copy-block or <h1>"
        })

    # --- P3.6 Title length gate (before wasting Verify quota) ---
    if len(title) > 80:
        return json.dumps({
            "error": f"derived title exceeds 80-char eBay limit "
            f"(got {len(title)}): {title!r}"
        })

    # --- P3.7 UUID cache per folder for retry idempotency ---
    cache_key = str(folder.resolve())
    if cache_key not in _create_listing_uuid_cache:
        _create_listing_uuid_cache[cache_key] = uuid.uuid4().hex.upper()
    uuid_hex = _create_listing_uuid_cache[cache_key]

    log_debug(
        f"create_listing folder={folder.name} oem={oem_model} condition={condition} "
        f"price={price} qty={quantity} dry_run={dry_run} uuid={uuid_hex}"
    )

    # --- P3.8 Photo resolution ---
    uploaded_urls: list[str] = []
    if picture_urls is None:
        if photo_paths is None:
            photo_paths = _glob_label_photos(folder)
        if not photo_paths:
            return json.dumps({
                "error": f"no IMG*.jpg photos found in {folder} and picture_urls not supplied"
            })
        # Internal call — reuse upload_photos MCP tool logic (keeps one code path).
        upload_json = await upload_photos(photo_paths, dry_run=False)
        upload_result = json.loads(upload_json)
        if not upload_result.get("success"):
            return json.dumps({
                "error": "photo upload failed",
                "uploaded_urls": upload_result.get("urls_uploaded_so_far", []),
                "failed_at_index": upload_result.get("failed_at_index"),
                "upload_error": upload_result.get("error"),
            })
        uploaded_urls = upload_result["urls"]
        picture_urls = uploaded_urls

    # --- P3.5 21-field ItemSpecifics ---
    item_specifics = _build_21_field_specifics(oem_model, title, has_caddy, specs)

    # --- P3.9 Build the Add payload ---
    payload = build_add_payload(
        title=title,
        description_html=resolved_html,
        price=price,
        quantity=quantity,
        condition_id=condition_id,
        condition_description=None,
        item_specifics=item_specifics,
        picture_urls=picture_urls,
        uuid_hex=uuid_hex,
    )

    try:
        # --- P3.10 Dry run → VerifyAddFixedPriceItem ---
        if dry_run:
            log_debug(f"VerifyAddFixedPriceItem CALLING uuid={uuid_hex}")
            response = await asyncio.to_thread(
                execute_with_retry, "VerifyAddFixedPriceItem", payload
            )
            reply = response.reply
            errors_raw = getattr(reply, "Errors", None)
            if errors_raw is None:
                errors = []
            elif isinstance(errors_raw, list):
                errors = [str(e) for e in errors_raw]
            else:
                errors = [str(errors_raw)]
            fees_raw = getattr(reply, "Fees", None)
            fees_summary = []
            if fees_raw is not None and hasattr(fees_raw, "Fee"):
                fee_list = fees_raw.Fee
                if not isinstance(fee_list, list):
                    fee_list = [fee_list]
                fees_summary = [
                    {
                        "name": str(f.Name),
                        "fee": str(getattr(f.Fee, "value", f.Fee)),
                        "currency": str(getattr(f.Fee, "_currencyID", "GBP")),
                    }
                    for f in fee_list
                ]
            log_debug(f"VerifyAddFixedPriceItem OK errors={len(errors)} fees={len(fees_summary)}")
            return json.dumps({
                "dry_run": True,
                "uuid": uuid_hex,
                "folder": str(folder),
                "oem_model": oem_model,
                "title": title,
                "picture_urls_count": len(picture_urls),
                "fees": fees_summary,
                "errors": errors,
                "payload_preview": {
                    "Quantity": payload["Item"]["Quantity"],
                    "StartPrice": payload["Item"]["StartPrice"],
                    "ConditionID": payload["Item"]["ConditionID"],
                    "ItemSpecifics_count": len(item_specifics),
                    "PictureURL_count": len(picture_urls),
                },
            }, indent=2)

        # --- P3.11 Apply → AddFixedPriceItem ---
        log_debug(f"AddFixedPriceItem CALLING uuid={uuid_hex}")
        response = await asyncio.to_thread(execute_with_retry, "AddFixedPriceItem", payload)
        reply = response.reply
        new_item_id = str(reply.ItemID)

        # UUID replay handling — eBay returns DuplicateInvocationDetails if this
        # UUID was already used to create a listing. That's a success, not an error.
        dup = getattr(reply, "DuplicateInvocationDetails", None)
        was_duplicate = dup is not None
        if was_duplicate:
            log_debug(
                f"AddFixedPriceItem DUPLICATE item_id={new_item_id} uuid={uuid_hex} "
                "— UUID replay succeeded, no new listing created"
            )
        else:
            log_debug(f"AddFixedPriceItem OK item_id={new_item_id} uuid={uuid_hex}")

        # Fees summary (same shape as dry-run)
        fees_raw = getattr(reply, "Fees", None)
        fees_summary = []
        if fees_raw is not None and hasattr(fees_raw, "Fee"):
            fee_list = fees_raw.Fee
            if not isinstance(fee_list, list):
                fee_list = [fee_list]
            fees_summary = [
                {
                    "name": str(f.Name),
                    "fee": str(getattr(f.Fee, "value", f.Fee)),
                    "currency": str(getattr(f.Fee, "_currencyID", "GBP")),
                }
                for f in fee_list
            ]

        # --- P3.12 Post-create verification ---
        verify_warnings: list[str] = []
        try:
            verify_resp = await asyncio.to_thread(
                execute_with_retry,
                "GetItem",
                {
                    "ItemID": new_item_id,
                    "DetailLevel": "ReturnAll",
                    "IncludeItemSpecifics": "true",
                },
            )
            if verify_resp.reply.Item is not None:
                landed = listing_to_dict(verify_resp.reply.Item)
                if landed["title"] != title:
                    verify_warnings.append(
                        f"title drift: payload={title!r} live={landed['title']!r}"
                    )
                if landed["quantity"] != int(quantity):
                    verify_warnings.append(
                        f"quantity drift: payload={quantity} live={landed['quantity']}"
                    )
                if landed["condition_id"] != str(condition_id):
                    verify_warnings.append(
                        f"condition drift: payload={condition_id} live={landed['condition_id']}"
                    )
                if len(landed["photos"]) != len(picture_urls):
                    verify_warnings.append(
                        f"picture count drift: payload={len(picture_urls)} "
                        f"live={len(landed['photos'])}"
                    )
                brand_live = landed["specifics"].get("Brand", [None])[0]
                if brand_live != specs["brand"]:
                    verify_warnings.append(
                        f"brand drift: payload={specs['brand']!r} live={brand_live!r}"
                    )
                mpn_live = landed["specifics"].get("MPN", [None])[0]
                if mpn_live != oem_model:
                    verify_warnings.append(
                        f"MPN drift: payload={oem_model!r} live={mpn_live!r}"
                    )
            else:
                verify_warnings.append("GetItem returned empty Item on post-create verify")
        except Exception as ve:
            verify_warnings.append(f"post-create GetItem failed: {ve}")

        # --- P3.13 Audit log ---
        audit_log_write(
            item_id=new_item_id,
            fields_changed=["CREATE"],
            before_length=0,
            after_length=len(resolved_html),
            success=True,
            local_html_path=str(folder),
            condition_after=str(condition_id),
        )

        # --- P3.14 Return shape ---
        listing_url = f"https://www.ebay.co.uk/itm/{new_item_id}"
        return json.dumps({
            "success": True,
            "item_id": new_item_id,
            "listing_url": listing_url,
            "uuid": uuid_hex,
            "duplicate_invocation": was_duplicate,
            "fees": fees_summary,
            "verify_warnings": verify_warnings,
            "before": None,
            "after": {
                "title": title,
                "oem_model": oem_model,
                "condition_id": condition_id,
                "quantity": quantity,
                "price": f"{price:.2f}",
                "picture_count": len(picture_urls),
            },
        }, indent=2)

    except Exception as e:
        # Preserve uploaded URLs in the error dict so caller can retry without
        # re-uploading (P3.14 requirement). @with_error_handling catches the
        # raise itself, but we layer the URL-preservation here first.
        if uploaded_urls:
            audit_log_write(
                item_id="(failed)",
                fields_changed=["CREATE"],
                before_length=0,
                after_length=len(resolved_html),
                success=False,
                error=str(e),
                local_html_path=str(folder),
            )
            return json.dumps({
                "success": False,
                "uuid": uuid_hex,
                "error": str(e),
                "uploaded_urls": uploaded_urls,
                "retry_hint": "call create_listing again with picture_urls=uploaded_urls",
            }, indent=2)
        raise


if __name__ == "__main__":
    log_debug("Starting ebay-seller-tool MCP server")
    mcp.run()
