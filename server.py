"""
ebay-seller-tool MCP server.

Provides tools for managing eBay listings from Claude Code.
Uses eBay Trading API (XML) for listing CRUD and photo uploads.
"""

import asyncio
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

from ebay.analytics import (  # noqa: E402
    _evaluate_wrong_direction_raise,
    compute_best_offer_thresholds,
    compute_funnel,
    compute_rank_health,
    diagnose_listing,
    price_verdict,
    sell_through_rate,
    summarise_feedback,
)
from ebay.analytics import (  # noqa: E402
    floor_price as compute_floor_price,
)
from ebay.auth import check_token_expiry, validate_credentials  # noqa: E402
from ebay.browse import fetch_competitor_prices  # noqa: E402
from ebay.client import execute_with_retry, log_debug, log_warn  # noqa: E402
from ebay.hdd_specs import HDD_SPECS  # noqa: E402
from ebay.listings import (  # noqa: E402
    MAX_PICTURE_URLS,
    MAX_PICTURE_URLS_JOINED_CHARS,
    audit_log_write,
    build_add_payload,
    build_revise_payload,
    compute_diff,
    extract_shipping_details,
    listing_to_dict,
    snapshot_listing,
)
from ebay.photos import (  # noqa: E402
    glob_visual_photos,
    preprocess_for_ebay,
    upload_one,
)
from ebay.end_listing import (  # noqa: E402
    ALLOWED_ENDING_REASONS,
    end_listing as _end_listing_core,
)
from ebay.pictures import revise_pictures as _revise_pictures_core  # noqa: E402
from ebay.rest import compute_return_rate as rest_compute_return_rate  # noqa: E402
from ebay.rest import (  # noqa: E402
    fetch_listing_returns,
    fetch_traffic_report,
    parse_traffic_report_response,
)
from ebay.selling import (  # noqa: E402
    fetch_listing_cases,
    fetch_listing_feedback,
    fetch_seller_transactions,
    fetch_sold_listings,
    fetch_unsold_listings,
)
from ebay.snapshots import append_snapshot, compute_elasticity  # noqa: E402
from ebay.store import fetch_store_info  # noqa: E402

# Single source of truth for the per-listing photo cap is ebay/listings.py —
# this alias keeps the old module-local name usable without duplicating the value.
MAX_PHOTOS_PER_LISTING = MAX_PICTURE_URLS
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
# VISUAL_PHOTO_PATTERNS + glob_visual_photos live in ebay/photos.py —
# audit scripts import them directly via sys.path injection (see
# audit_smart_visuals.py) without pulling FastMCP startup.
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
        raise FileNotFoundError(f"no listing HTML in {folder} and no template at {template_path}")
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


_REQUIRED_SPEC_FIELDS = ("brand", "family", "capacity", "rpm", "interface", "form_factor", "cache")


def _build_21_field_specifics(
    oem_model: str,
    title: str,
    has_caddy: bool,
    specs: dict[str, str | None],
) -> dict[str, str | list[str]]:
    """Canonical 21-field ItemSpecifics per research §1.3.

    Required-field contract: every HDD_SPECS entry has non-None values for
    brand/family/capacity/rpm/interface/form_factor/cache (enforced by the
    P1.9 seed test). `height` may be None for 3.5" drives only.

    `Transfer Rate` is title-authoritative per P3.5; the HDD_SPECS
    `transfer_rate` field exists as a catalogue reference and is NOT read
    here — title is the ground truth for 12G vs 6G vs 3G.
    """
    missing = [k for k in _REQUIRED_SPEC_FIELDS if not specs.get(k)]
    if missing:
        raise ValueError(
            f"HDD_SPECS[{oem_model!r}] has empty/None required field(s): {missing}. "
            "Fix ebay/hdd_specs.py before creating listing."
        )
    storage_format = "HDD with Caddy" if has_caddy else "HDD Only"
    specifics: dict[str, str | list[str]] = {
        "Brand": specs["brand"],
        "MPN": oem_model,
        "Model": oem_model,
        "Product Line": specs["family"],
        "Type": "Internal Hard Drive",
        "Drive Type(s) Supported": "HDD",
        "Storage Format": storage_format,
        "Storage Capacity": specs["capacity"],
        "Interface": specs["interface"],
        "Form Factor": specs["form_factor"],
        "Rotation Speed": specs["rpm"],
        "Cache": specs["cache"],
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
    height = specs.get("height")
    if height:
        specifics["Height"] = height
    return specifics


def _glob_label_photos(folder: Path) -> list[str]:
    """Find Hoi's phone-camera label photos in a product folder.

    Glob case-insensitively — iPhones and some Android transfers produce
    uppercase .JPG while Hoi's primary Android writes .jpg. Dedup protects
    against case-insensitive filesystems (WSL, macOS) that match both
    patterns for the same file.
    """
    if not folder.exists():
        return []
    seen: set[str] = set()
    results: list[str] = []
    for pattern in ("IMG*.jpg", "IMG*.JPG"):
        for p in sorted(folder.glob(pattern)):
            s = str(p)
            if s in seen:
                continue
            seen.add(s)
            if LABEL_PHOTO_REGEX.search(p.name):
                results.append(s)
    return results


def _warn_missing_oauth_vars() -> None:
    """Issue #5 Phase 2: additive diagnostic for OAuth-gated tools.

    fail-fast on first call to the OAuth-gated tools is preserved (oauth.py
    + browse.py both raise PermissionError with explicit env-var names).
    This warning is purely discoverability — surfaces the degraded state
    at boot so the operator knows BEFORE reaching for an OAuth tool.
    """
    runtime_required = {
        "EBAY_APP_CLIENT_ID": "Browse + Traffic Report + Post-Order v2 OAuth app auth",
        "EBAY_APP_CLIENT_SECRET": "Browse + Traffic Report + Post-Order v2 OAuth app auth",
        "EBAY_OWN_SELLER_USERNAME": "own-seller exclusion in find_competitor_prices",
    }
    missing = [k for k in runtime_required if not os.environ.get(k)]
    if not missing:
        return
    gated_tools = [
        "find_competitor_prices",
        "get_traffic_report",
        "get_listing_returns",
        "compute_return_rate",
    ]
    log_debug(
        "OAuth env vars missing="
        + ",".join(missing)
        + " — gated_tools_unavailable="
        + ",".join(gated_tools)
        + " — fail-fast on first call preserved; see .env.example"
    )


mcp = FastMCP("ebay-seller-tool")


# Suppress ebaysdk logging unless EBAY_DEBUG=1
if not os.environ.get("EBAY_DEBUG"):
    logging.getLogger("ebaysdk").setLevel(logging.CRITICAL)

# Validate credentials at module load (runs whether invoked via __main__ or MCP framework)
validate_credentials()
check_token_expiry()
_warn_missing_oauth_vars()


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
                # WatchCount needs explicit opt-in (DetailLevel=ReturnAll omits it).
                "IncludeWatchCount": "true",
            },
            # AC 1.13 Phase 1 sample invocation revealed ReturnPolicy + full
            # ShippingDetails are omitted in the default response. ReturnAll
            # surfaces those plus other extended fields needed by listing_to_dict.
            "DetailLevel": "ReturnAll",
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
        # Re-use listing_to_dict for extended fields (Issue #4 Phase 1.2).
        # Strip description_html/specifics to keep the listing-table shape lean.
        full = listing_to_dict(item)
        selling_status = item.SellingStatus
        listing = {
            "item_id": full["item_id"],
            "title": full["title"],
            "price": {
                "amount": str(selling_status.CurrentPrice.value),
                "currency": str(selling_status.CurrentPrice._currencyID),
            },
            "quantity_available": int(item.QuantityAvailable),
            "quantity_sold": full["quantity_sold"],
            "watch_count": full["watch_count"],
            "view_count": full["view_count"],
            "best_offer_count": full["best_offer_count"],
            "best_offer_enabled": full["best_offer_enabled"],
            "question_count": full["question_count"],
            "relist_count": full["relist_count"],
            "start_time": full["start_time"],
            "end_time": full["end_time"],
            "days_on_site": full["days_on_site"],
            "shipping": full["shipping"],
            "return_policy": full["return_policy"],
            "listing_url": full["listing_url"],
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
        {
            "ItemID": item_id,
            "DetailLevel": "ReturnAll",
            "IncludeItemSpecifics": "true",
            # WatchCount needs explicit opt-in (DetailLevel=ReturnAll omits it).
            "IncludeWatchCount": "true",
        },
    )

    if response.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})

    result = listing_to_dict(response.reply.Item)
    log_debug(
        f"get_listing_details OK item_id={item_id} title={result['title'][:50]} "
        f"desc_len={result['description_length']}"
    )
    return json.dumps(result, indent=2)


async def _measure_or_default_floor(item_id: str) -> tuple[dict, str]:
    """Return (floor_price_result, return_rate_source).

    Phase 4.1: prefer live compute_return_rate if available; fall back to defaults
    if OAuth not configured or measurement fails.
    """
    try:
        rr = await rest_compute_return_rate(item_id=item_id, days=90)
        if rr.get("return_rate_pct") is not None:
            return (
                compute_floor_price(return_rate=float(rr["return_rate_pct"]) / 100.0),
                "measured (Phase 2, 90d)",
            )
    except Exception as e:
        # Documented fail-soft: guardrail must not block legitimate updates due
        # to a transient measurement issue (timeout, 5xx, unexpected response
        # shape). The default floor is always safe; measurement is best-effort.
        log_debug(
            f"floor_guard measured_rate_unavailable item_id={item_id} "
            f"reason={type(e).__name__}: {e}"
        )
    return compute_floor_price(), "default"


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
    best_offer_enabled: bool | None = None,
    best_offer_auto_accept_gbp: float | None = None,
    best_offer_auto_decline_gbp: float | None = None,
    dry_run: bool = False,
    current_analysis: dict | None = None,
) -> str:
    """Update any listing field except quantity on an existing listing.

    Supports: title, description, price, condition, condition description,
    item specifics, and Best Offer toggle + auto-accept/decline thresholds.
    Quantity is intentionally blocked.

    Phase 4 guardrail: when `price` is provided, the tool refuses to revise
    to a price below the computed floor (config/fees.yaml + measured per-SKU
    return rate if Phase 2 OAuth available). The same guardrail extends to
    `best_offer_auto_accept_gbp` — accepting offers below break-even is the
    same loss as listing below it. Raise is loud — no silent clamp.

    Args:
        item_id: The eBay item ID to update.
        title: New title (max 80 chars). Optional.
        description_html: New description HTML. Optional.
        price: New price (must be > 0). Optional.
        condition_id: eBay condition ID (values from CONDITION_MAP —
            1000=New, 1500=Opened, 2750=Used - Excellent, 3000=Used). Optional.
        condition_description: Seller notes text for condition. Optional.
        item_specifics: Dict of name->value(s) for item specifics. Optional.
        best_offer_enabled: Toggle Best Offer on the listing. Optional.
        best_offer_auto_accept_gbp: Offers >= this price auto-accept. Must
            be >= the floor and >= best_offer_auto_decline_gbp. Optional.
        best_offer_auto_decline_gbp: Offers below this price auto-decline.
            Maps to MinimumBestOfferPrice (the auto-decline-below threshold).
            Optional.
        dry_run: If True, return diff without making changes. Default False.
        current_analysis: Optional — prior analyse_listing(item_id) output. If
            supplied with dry_run=True, the dry-run response echoes it back
            verbatim (no API re-fetch). Keeps update_listing decoupled from
            analyse_listing.

    Returns:
        JSON with diff (dry_run) or success result with before/after snapshots.
    """
    if not item_id or not item_id.strip():
        return json.dumps({"error": "item_id required"})

    updatable = [
        title,
        description_html,
        price,
        condition_id,
        condition_description,
        item_specifics,
        best_offer_enabled,
        best_offer_auto_accept_gbp,
        best_offer_auto_decline_gbp,
    ]
    has_update = any(v is not None for v in updatable)
    if not has_update:
        return json.dumps({"error": "no fields to update"})

    if title is not None and len(title) > 80:
        return json.dumps({"error": f"title exceeds 80-char eBay limit (got {len(title)})"})
    if price is not None and price <= 0:
        return json.dumps({"error": "price must be > 0"})
    if best_offer_auto_accept_gbp is not None and best_offer_auto_accept_gbp <= 0:
        return json.dumps({"error": "best_offer_auto_accept_gbp must be > 0"})
    if best_offer_auto_decline_gbp is not None and best_offer_auto_decline_gbp <= 0:
        return json.dumps({"error": "best_offer_auto_decline_gbp must be > 0"})
    if (
        best_offer_auto_accept_gbp is not None
        and best_offer_auto_decline_gbp is not None
        and best_offer_auto_accept_gbp < best_offer_auto_decline_gbp
    ):
        return json.dumps(
            {
                "error": (
                    f"best_offer_auto_accept_gbp ({best_offer_auto_accept_gbp:.2f}) "
                    f"< best_offer_auto_decline_gbp ({best_offer_auto_decline_gbp:.2f}) — "
                    "eBay rejects payloads where auto-accept lands below the "
                    "MinimumBestOfferPrice (auto-decline floor)."
                )
            }
        )
    if condition_id is not None and condition_id not in set(CONDITION_MAP.values()):
        return json.dumps(
            {
                "error": f"invalid condition_id {condition_id}. Valid: "
                f"{sorted((v, k) for k, v in CONDITION_MAP.items())}. "
                "7000 (For parts) is blocked — use eBay Seller Hub directly.",
            }
        )
    if description_html is not None:
        description_html = description_html.strip()
        if not description_html:
            return json.dumps({"error": "description_html must not be empty"})
        if "<" not in description_html or ">" not in description_html:
            return json.dumps({"error": "description_html must contain at least one HTML tag"})

    update_fields = [
        f
        for f in [
            "title",
            "description_html",
            "price",
            "condition_id",
            "condition_description",
            "item_specifics",
            "best_offer_enabled",
            "best_offer_auto_accept_gbp",
            "best_offer_auto_decline_gbp",
        ]
        if locals().get(f) is not None
    ]
    log_debug(f"update_listing item_id={item_id} dry_run={dry_run} fields={update_fields}")

    # Fetch current state for diff
    current = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {
            "ItemID": item_id,
            "DetailLevel": "ReturnAll",
            "IncludeItemSpecifics": "true",
            # WatchCount needs explicit opt-in (DetailLevel=ReturnAll omits it).
            "IncludeWatchCount": "true",
        },
    )
    if current.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})

    # Single listing_to_dict call — used for snapshot, diff, AND specifics merge
    current_full = listing_to_dict(current.reply.Item)
    before = snapshot_listing(current.reply.Item)

    diff = compute_diff(
        before,
        title,
        description_html,
        price,
        condition_id,
        condition_description,
        item_specifics,
    )

    # Best Offer fields aren't part of compute_diff (which lives in
    # ebay/listings.py and is shared with snapshot_listing). Surface them as
    # additional diff entries here. `best_offer_enabled` compares against
    # current_full; auto-accept / auto-decline have no GetItem-surfaced prior
    # state in our serialiser, so we record only the after value.
    if best_offer_enabled is not None:
        before_be = current_full.get("best_offer_enabled")
        if before_be != best_offer_enabled:
            diff["best_offer_enabled"] = {"before": before_be, "after": best_offer_enabled}
    if best_offer_auto_accept_gbp is not None:
        diff["best_offer_auto_accept_gbp"] = {"after": best_offer_auto_accept_gbp}
    if best_offer_auto_decline_gbp is not None:
        diff["best_offer_auto_decline_gbp"] = {"after": best_offer_auto_decline_gbp}

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

    # Phase 4 floor-price guardrail (AC 4.1) — applies to listing price AND
    # best_offer_auto_accept_gbp (auto-accepting offers below break-even is
    # the same loss as listing below break-even).
    floor_payload: dict[str, object] | None = None
    if price is not None or best_offer_auto_accept_gbp is not None:
        floor_result, rate_source = await _measure_or_default_floor(item_id)
        floor_gbp = float(floor_result["floor_gbp"])
        ceiling_gbp = float(floor_result["suggested_ceiling_gbp"])
        return_rate = float(floor_result["inputs"]["return_rate"])
        cogs_gbp = float(floor_result["inputs"]["cogs_gbp"])
        verdict_price = price if price is not None else float(current_full["price"])
        verdict = price_verdict(
            current_price=verdict_price,
            floor=floor_gbp,
            return_rate=return_rate,
            source=rate_source,
        )
        floor_payload = {
            "floor_gbp": floor_gbp,
            "suggested_ceiling_gbp": ceiling_gbp,
            "return_rate_source": rate_source,
            "price_verdict": verdict,
        }
        if price is not None and price < floor_gbp:
            error_msg = (
                f"Price £{price:.2f} below floor £{floor_gbp:.2f} "
                f"(source: config/fees.yaml — COGS £{cogs_gbp:.2f}, return rate "
                f"{return_rate:.1%} [{rate_source}]). "
                "Raise price or revisit return-rate/margin assumptions."
            )
            return json.dumps(
                {"error": error_msg, "floor_gbp": floor_gbp, "requested_price": price},
                indent=2,
            )
        if best_offer_auto_accept_gbp is not None and best_offer_auto_accept_gbp < floor_gbp:
            error_msg = (
                f"best_offer_auto_accept_gbp £{best_offer_auto_accept_gbp:.2f} below "
                f"floor £{floor_gbp:.2f} (source: config/fees.yaml — COGS "
                f"£{cogs_gbp:.2f}, return rate {return_rate:.1%} [{rate_source}]). "
                "Auto-accepting at this threshold loses money on every accepted offer."
            )
            return json.dumps(
                {
                    "error": error_msg,
                    "floor_gbp": floor_gbp,
                    "requested_auto_accept": best_offer_auto_accept_gbp,
                },
                indent=2,
            )

    # Issue #14 Phase 3 — wrong-direction-raise WARN evaluation. Runs BEFORE
    # ReviseFixedPriceItem so the WARN appears even when dry_run=True; that
    # way operator sees the signal at decision time, not after the fact.
    # Observability ONLY — does NOT block the change. The helper short-
    # circuits to None when there's no usable signal (no recent sales / no
    # comp pool / BELOW_P25 / stock_clearance_exempt) so spurious WARNs are
    # avoided.
    wrong_direction_warning: dict | None = None
    if price is not None and "price" in diff:
        try:
            old_price_val = float(before.get("price")) if before.get("price") else None
        except (TypeError, ValueError):
            old_price_val = None
        if old_price_val is not None and price > old_price_val:
            try:
                from ebay.fees import _load_fees_config  # noqa: PLC0415

                cfg_wd = _load_fees_config().get("wrong_direction_warn", {}) or {}
                wd_window_days = int(cfg_wd.get("sales_window_days", 14))
                wd_min_units = int(cfg_wd.get("min_units_sold", 1))
                wrong_direction_warning = await _evaluate_wrong_direction_raise(
                    item_id=str(item_id),
                    old_price=old_price_val,
                    new_price=float(price),
                    item_full=current_full,
                    sales_window_days=wd_window_days,
                    min_units_sold=wd_min_units,
                )
                if wrong_direction_warning is not None:
                    log_warn(
                        f"WRONG_DIRECTION_RAISE item={item_id} "
                        f"old=£{old_price_val:.2f} new=£{price:.2f} "
                        f"units_sold_{wd_window_days}d="
                        f"{wrong_direction_warning['units_sold']} "
                        f"watch_count={wrong_direction_warning['watch_count']} "
                        f"recommendation={wrong_direction_warning['recommendation']}"
                    )
            except (KeyError, ValueError, TypeError) as e:
                log_debug(
                    f"update_listing wrong_direction_eval_failed item_id={item_id} "
                    f"reason={type(e).__name__}: {e}"
                )

    if dry_run:
        dry_response: dict[str, object] = {"dry_run": True, "item_id": item_id, "diff": diff}
        if floor_payload is not None:
            dry_response.update(floor_payload)
        if current_analysis is not None:
            dry_response["current_analysis"] = current_analysis
        if wrong_direction_warning is not None:
            dry_response["wrong_direction_warning"] = wrong_direction_warning
        return json.dumps(dry_response, indent=2)

    # Build and send ReviseFixedPriceItem payload — echo back current shipping config
    shipping = extract_shipping_details(current.reply.Item)

    # Item specifics: eBay replaces the entire block, so merge new values into existing
    merged_specifics = None
    if item_specifics is not None:
        merged_specifics = dict(current_full.get("specifics", {}))
        for k, v in item_specifics.items():
            merged_specifics[k] = v if isinstance(v, list) else [v]

    listing_currency = current_full.get("currency") or "GBP"
    payload = build_revise_payload(
        item_id=item_id,
        title=title,
        description_html=description_html,
        price=price,
        shipping_details=shipping,
        condition_id=condition_id,
        condition_description=condition_description,
        item_specifics=merged_specifics,
        best_offer_enabled=best_offer_enabled,
        best_offer_auto_accept_gbp=best_offer_auto_accept_gbp,
        best_offer_auto_decline_gbp=best_offer_auto_decline_gbp,
        currency=listing_currency,
    )
    await asyncio.to_thread(execute_with_retry, "ReviseFixedPriceItem", payload)

    # Verify by re-fetching
    after_resp = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {
            "ItemID": item_id,
            "DetailLevel": "ReturnAll",
            "IncludeItemSpecifics": "true",
            # WatchCount needs explicit opt-in (DetailLevel=ReturnAll omits it).
            "IncludeWatchCount": "true",
        },
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

    success_response: dict[str, object] = {
        "success": True,
        "item_id": item_id,
        "fields_updated": list(diff.keys()),
        "before": before,
        "after": after,
    }
    if floor_payload is not None:
        success_response["floor_verdict"] = floor_payload["price_verdict"]
        success_response["return_rate_source"] = floor_payload["return_rate_source"]
    if wrong_direction_warning is not None:
        success_response["wrong_direction_warning"] = wrong_direction_warning

    # Phase 5.2.2 — emit price_change snapshot only when price actually changed.
    if "price" in diff:
        try:
            old_price_str = before.get("price")
            try:
                old_price = float(old_price_str) if old_price_str is not None else None
            except (TypeError, ValueError):
                old_price = None
            append_snapshot(
                "price_change",
                str(item_id),
                {
                    "price_gbp": price,
                    "old_price_gbp": old_price,
                    "quantity": current_full.get("quantity"),
                    "watch_count": current_full.get("watch_count"),
                    "view_count": current_full.get("view_count"),
                    "traffic_30d": None,
                    "source": "update_listing",
                },
            )
        except (OSError, ValueError) as e:
            # JSONL append failure is a data-loss path — get_elasticity (Phase 2)
            # will return insufficient_events without operator visibility into why.
            # Bump to log_warn so the operator sees the broken ledger entry.
            log_warn(
                f"update_listing snapshot_append_failed item_id={item_id} "
                f"reason={type(e).__name__}: {e}"
            )

        # Issue #14 Phase 1 — emit post_change_check from the existing
        # post-revise GetItem response. snapshot_listing(after) lacks
        # watch_count + view_count (see ebay/listings.py:275-288), so derive
        # the post-change state from listing_to_dict(after_resp.reply.Item)
        # which exposes both. No new GetItem call, no asyncio.sleep — the
        # existing post-revise GetItem at server.py already requested
        # IncludeWatchCount=true. Latency unchanged.
        # FRESHNESS NOTE: this immediate snapshot is captured ~3-5s
        # post-ReviseFixedPriceItem before watcher counts can shift;
        # get_elasticity (Phase 2) emits a freshness_warning when the
        # before/after pair is <7d apart.
        try:
            after_full = listing_to_dict(after_resp.reply.Item)
            append_snapshot(
                "post_change_check",
                str(item_id),
                {
                    "price_gbp": price,
                    "quantity": after_full.get("quantity"),
                    "watch_count": after_full.get("watch_count"),
                    "view_count": after_full.get("view_count"),
                    "traffic_30d": None,
                    "source": "update_listing",
                },
            )
        except (OSError, ValueError) as e:
            # Same data-loss rationale as price_change above — Phase 2
            # get_elasticity depends on this snapshot landing.
            log_warn(
                f"update_listing post_change_check_append_failed item_id={item_id} "
                f"reason={type(e).__name__}: {e}"
            )

    return json.dumps(success_response, indent=2)


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
        return json.dumps(
            {
                "error": (
                    f"photo_paths exceeds eBay {MAX_PHOTOS_PER_LISTING}-image cap "
                    f"(got {len(photo_paths)})"
                )
            }
        )

    log_debug(f"upload_photos count={len(photo_paths)} dry_run={dry_run}")

    if dry_run:
        preview = []
        for p in photo_paths:
            try:
                bytes_out = await asyncio.to_thread(preprocess_for_ebay, p)
                preview.append(
                    {
                        "path": p,
                        "size_bytes_after_preprocess": len(bytes_out),
                        "rejected": False,
                    }
                )
            except ValueError as e:
                preview.append({"path": p, "rejected": True, "reason": str(e)})
        return json.dumps(
            {
                "dry_run": True,
                "would_upload": len([p for p in preview if not p["rejected"]]),
                "preview": preview,
            },
            indent=2,
        )

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
            return json.dumps(
                {
                    "success": False,
                    "urls_uploaded_so_far": urls,
                    "failed_at_index": idx,
                    "failed_path": p,
                    "error": str(e),
                },
                indent=2,
            )

    total_chars = sum(len(u) for u in urls)
    warnings: list[str] = []
    if total_chars >= MAX_PICTURE_URLS_JOINED_CHARS:
        warnings.append(
            f"total_url_chars={total_chars} exceeds eBay soft cap "
            f"{MAX_PICTURE_URLS_JOINED_CHARS} — listing may reject PictureDetails"
        )

    return json.dumps(
        {
            "success": True,
            "urls": urls,
            "total_url_chars": total_chars,
            "warnings": warnings,
        },
        indent=2,
    )


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
        photo_paths: Ordered list of photo paths. If None, glob IMG*.jpg and
            IMG*.JPG from folder (case-insensitive).
        description_html: Override the HTML file in the folder. If None, the
            tool resolves listing-<suffix>.html → listing.html → Jinja template.
        dry_run: If True, call VerifyAddFixedPriceItem (default). If False,
            call AddFixedPriceItem for real.
        picture_urls: Skip upload and use these URLs. If None, tool calls
            upload_photos internally — this uploads regardless of dry_run
            because VerifyAddFixedPriceItem still needs real PictureURLs.
            To do a pure dry-run without uploads, pre-upload separately and
            pass the URL list here, OR mock the uploads.

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
        return json.dumps(
            {"error": f"invalid condition {condition!r}. Valid: {sorted(CONDITION_MAP.keys())}"}
        )
    if not isinstance(has_caddy, bool):
        return json.dumps({"error": "has_caddy must be bool"})
    if photo_paths is not None and not photo_paths:
        return json.dumps({"error": "photo_paths provided but empty"})

    condition_id = CONDITION_MAP[condition]
    oem_model = _extract_oem_model(folder_path)

    # --- P3.4 HDD_SPECS lookup (fail loud on unknown MPN) ---
    if oem_model not in HDD_SPECS:
        return json.dumps(
            {"error": f"Unknown MPN {oem_model}. Add to ebay/hdd_specs.py before creating listing."}
        )
    specs = HDD_SPECS[oem_model]

    # --- P3.3 HTML + title resolution ---
    try:
        resolved_html = _resolve_description_html(folder, condition, description_html)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)})

    title = _extract_title_from_html(resolved_html)
    if title is None:
        return json.dumps(
            {
                "error": "could not derive title from HTML (no copy-block, no <h1>); "
                "provide description_html with a copy-block or <h1>"
            }
        )

    # --- P3.6 Title length gate (before wasting Verify quota) ---
    if len(title) > 80:
        return json.dumps(
            {"error": f"derived title exceeds 80-char eBay limit (got {len(title)}): {title!r}"}
        )

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
    label_photos: list[str] = []
    visual_photos: list[str] = []
    photo_warnings: list[str] = []
    if picture_urls is None:
        if photo_paths is None:
            # #25 triple-glob: regular IMG photos first (gallery image preserved
            # at index 0), then SMART-test visuals appended in pattern order.
            label_photos = _glob_label_photos(folder)
            visual_photos = glob_visual_photos(folder)
            photo_paths = label_photos + visual_photos
            log_debug(
                f"create_listing photo_glob folder={folder.name} "
                f"label_count={len(label_photos)} visual_count={len(visual_photos)}"
            )
        if not photo_paths:
            return json.dumps(
                {
                    "error": (
                        f"no IMG*.jpg or visual-*/SMART-*/DISK-TEST-VISUAL-*.png "
                        f"photos found in {folder} and picture_urls not supplied"
                    )
                }
            )
        if len(photo_paths) > MAX_PHOTOS_PER_LISTING:
            return json.dumps(
                {
                    "error": (
                        f"resolved photo set ({len(photo_paths)}) exceeds eBay "
                        f"{MAX_PHOTOS_PER_LISTING}-image cap — supply photo_paths "
                        "explicitly to choose which to keep."
                    )
                }
            )
        # 3-sample-minimum rule for multi-qty listings (#25 AC). Bulk-test
        # workflow rule: at qty>1 we expect >=3 SMART visuals so reviewers can
        # see distinct drives' health evidence. Warn-then-proceed (not refuse) —
        # qty>1 listings can still launch on visual-light evidence; the warning
        # surfaces in the response so the operator can backfill before next
        # bulk batch.
        if quantity > 1 and len(visual_photos) < 3:
            photo_warnings.append(
                f"multi-qty listing (quantity={quantity}) has only "
                f"{len(visual_photos)} SMART visuals — bulk-test workflow "
                "expects >=3 distinct visuals at qty>1 (skill SKILL.md "
                "Bulk-test workflow). Proceeding; backfill before next batch."
            )
        # Internal call — reuse upload_photos MCP tool logic (keeps one code path).
        upload_json = await upload_photos(photo_paths, dry_run=False)
        upload_result = json.loads(upload_json)
        if not upload_result.get("success"):
            return json.dumps(
                {
                    "error": "photo upload failed",
                    "uploaded_urls": upload_result.get("urls_uploaded_so_far", []),
                    "failed_at_index": upload_result.get("failed_at_index"),
                    "upload_error": upload_result.get("error"),
                }
            )
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
            return json.dumps(
                {
                    "dry_run": True,
                    "uuid": uuid_hex,
                    "folder": str(folder),
                    "oem_model": oem_model,
                    "title": title,
                    "picture_urls_count": len(picture_urls),
                    # #25 AC: dry-run preview shows visual photos distinctly
                    # from IMG photos. Counts come from the triple-glob
                    # resolution; when the caller supplied photo_paths /
                    # picture_urls explicitly, both are 0 (we don't classify
                    # operator-supplied paths since they may be neither).
                    "label_photo_count": len(label_photos),
                    "visual_photo_count": len(visual_photos),
                    "photo_warnings": photo_warnings,
                    "fees": fees_summary,
                    "errors": errors,
                    "payload_preview": {
                        "Quantity": payload["Item"]["Quantity"],
                        "StartPrice": payload["Item"]["StartPrice"],
                        "ConditionID": payload["Item"]["ConditionID"],
                        "ItemSpecifics_count": len(item_specifics),
                        "PictureURL_count": len(picture_urls),
                    },
                },
                indent=2,
            )

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
                    # WatchCount needs explicit opt-in (DetailLevel=ReturnAll omits it).
                    "IncludeWatchCount": "true",
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
                    verify_warnings.append(f"MPN drift: payload={oem_model!r} live={mpn_live!r}")
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
        # M7 fix (Ralph deferred Opus, completed): prefer the server-authoritative
        # ListingDetails.ViewItemURL captured by `landed = listing_to_dict(...)`
        # during the post-create verify above. The verify block can fail (no
        # `landed` defined), in which case fall back to the marketplace literal —
        # the listing was created so we still have the item_id; the URL fallback
        # only matters for the operator preview.
        listing_url = (
            landed["listing_url"]  # type: ignore[possibly-undefined]
            if "landed" in locals() and isinstance(landed, dict) and landed.get("listing_url")
            else f"https://www.ebay.co.uk/itm/{new_item_id}"
        )
        return json.dumps(
            {
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
            },
            indent=2,
        )

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
            return json.dumps(
                {
                    "success": False,
                    "uuid": uuid_hex,
                    "error": str(e),
                    "uploaded_urls": uploaded_urls,
                    "retry_hint": "call create_listing again with picture_urls=uploaded_urls",
                },
                indent=2,
            )
        raise


@mcp.tool()
@with_error_handling
async def get_sold_listings(days: int = 30, page: int = 1, per_page: int = 25) -> str:
    """Get recently SOLD listings from the seller's eBay store.

    Wraps GetMyeBaySelling.SoldList. Useful for days-to-sell and sell-through
    analytics. Read-only.

    Args:
        days: Lookback window in days (1-60). Default 30.
        page: Page number (1-based). Default 1.
        per_page: Entries per page (1-200). Default 25.

    Returns:
        JSON {total, page, per_page, listings[]} — each listing has item_id,
        title, sold_price, quantity_sold, start_time, end_time, days_live,
        best_offer_count, watch_count.
    """
    log_debug(f"get_sold_listings days={days} page={page} per_page={per_page}")
    result = await fetch_sold_listings(days=days, page=page, per_page=per_page)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def get_unsold_listings(days: int = 60, page: int = 1, per_page: int = 25) -> str:
    """Get recently ended UNSOLD listings (GTC no-sale).

    Wraps GetMyeBaySelling.UnsoldList. Feeds sell-through-rate computation.

    Args:
        days: Lookback window in days (1-60). Default 60.
        page: Page number (1-based). Default 1.
        per_page: Entries per page (1-200). Default 25.
    """
    log_debug(f"get_unsold_listings days={days} page={page} per_page={per_page}")
    result = await fetch_unsold_listings(days=days, page=page, per_page=per_page)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def get_seller_transactions(days: int = 30, page: int = 1) -> str:
    """Get line-item transactions (paid orders) for the seller.

    Wraps GetSellerTransactions. Provides per-transaction created/paid/shipped
    timestamps and a derived days_to_sell metric.

    Args:
        days: Lookback window in days (1-30 — API max). Default 30.
        page: Page number. Default 1.
    """
    log_debug(f"get_seller_transactions days={days} page={page}")
    result = await fetch_seller_transactions(days=days, page=page)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def get_listing_feedback(item_id: str, days: int = 90) -> str:
    """Get per-transaction buyer feedback for one listing.

    Wraps GetFeedback(ItemID=X). Returns comments + Detailed Seller Ratings.
    Aggregated `dsr_item_as_described_avg` is the explicit source for
    analyse_listing's signals.dsr_item_as_described.

    Args:
        item_id: eBay item ID.
        days: Filter window in days. Default 90.
    """
    log_debug(f"get_listing_feedback item_id={item_id} days={days}")
    result = await fetch_listing_feedback(item_id=item_id, days=days)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def get_listing_cases(item_id: str, days: int = 90) -> str:
    """Get open + closed resolution cases for one listing.

    Wraps getUserCases with EBP_INR + EBP_SNAD filter. Diagnostic only —
    the MCP never auto-responds to cases (never-dispute-customer rule).

    Args:
        item_id: eBay item ID.
        days: Lookback window (1-90). Default 90.
    """
    log_debug(f"get_listing_cases item_id={item_id} days={days}")
    result = await fetch_listing_cases(item_id=item_id, days=days)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def floor_price(
    cogs: float | None = None,
    return_rate: float | None = None,
    postage_out: float | None = None,
    postage_return: float | None = None,
    packaging: float | None = None,
    time_sale_gbp: float | None = None,
    time_return_gbp: float | None = None,
    fvf_rate: float | None = None,
    per_order_fee: float | None = None,
    target_margin: float | None = None,
    postage_charged: float = 0.0,
) -> str:
    """Compute the break-even floor price for a listing.

    All `None` defaults read from config/fees.yaml at call-time. Override
    any parameter by passing a concrete value.

    Formula:
        fixed = cogs + per_order_fee + packaging + postage_out + time_sale
        return_extra = postage_return + time_return
        floor = (fixed + p*return_extra + (1-p)*fvf*postage_charged)
              / ((1-p)*(1-fvf) - target_margin)

    Returns:
        JSON {floor_gbp, suggested_ceiling_gbp, inputs}.
    """
    log_debug(f"floor_price cogs={cogs} return_rate={return_rate} target_margin={target_margin}")
    result = compute_floor_price(
        cogs=cogs,
        return_rate=return_rate,
        postage_out=postage_out,
        postage_return=postage_return,
        packaging=packaging,
        time_sale_gbp=time_sale_gbp,
        time_return_gbp=time_return_gbp,
        fvf_rate=fvf_rate,
        per_order_fee=per_order_fee,
        target_margin=target_margin,
        postage_charged=postage_charged,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def analyse_listing(
    item_id: str,
    window_days: int = 30,
    include_cases: bool = False,
    include_market_concentration: bool = False,
) -> str:
    """Diagnose a listing: funnel + signals + decision matrix + floor/ceiling.

    Combines extended listing detail, seller transactions, buyer feedback
    (+ optional resolution cases) and maps the result to a recommended action.

    Args:
        item_id: eBay item ID.
        window_days: Signals window in days. Default 30.
        include_cases: If True, call getUserCases for resolution-case data.
            Default False (avoids extra API call at 10-listing scale).
        include_market_concentration: Stub #19. If True, fetch the comp pool
            and surface `comp_pool_stats: {top_seller_pct, distinct_sellers,
            herfindahl, confidence}` in the response. Default False — the comp
            scan is the slowest call in this tool, so opt-in only when the
            caller actually needs the concentration signal (weekly sweep).

    Returns:
        JSON with funnel / signals / multi_qty_note / rank_health_status /
        diagnosis / recommended_action / floor_price_gbp / suggested_ceiling_gbp /
        current_price_gbp / price_verdict.

        Phase 2 fills funnel.impressions, funnel.ctr_pct,
        signals.sales_conversion_rate_pct, and signals.return_rate_pct.
    """
    if not item_id or not item_id.strip():
        return json.dumps({"error": "item_id required"})

    log_debug(
        f"analyse_listing item_id={item_id} window_days={window_days} include_cases={include_cases}"
    )

    # 1. Extended listing detail (reuses ebay/listings.py single-source serialisation).
    get_item_response = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {
            "ItemID": item_id,
            "DetailLevel": "ReturnAll",
            "IncludeItemSpecifics": "true",
            # WatchCount needs explicit opt-in (DetailLevel=ReturnAll omits it).
            # This is THE call that produced the "0 watchers across all listings" audit artefact.
            "IncludeWatchCount": "true",
        },
    )
    if get_item_response.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})
    listing = listing_to_dict(get_item_response.reply.Item)

    # 2. Seller transactions in window → days-to-sell distribution.
    # G-NEW-3: surface p25/p50/p75/n_samples instead of bare median — distribution
    # shape matters for under/over-pricing decisions per 13_ANALYTICS_AND_PRICING.md §2.3.
    txns_result = await fetch_seller_transactions(days=min(window_days, 30))
    per_item_txns = [t for t in txns_result["transactions"] if t["item_id"] == str(item_id)]
    days_to_sell_values = [
        t["days_to_sell"] for t in per_item_txns if t["days_to_sell"] is not None
    ]
    days_to_sell_n_samples = len(days_to_sell_values)
    days_to_sell_median: float | None = None
    days_to_sell_p25: float | None = None
    days_to_sell_p50: float | None = None
    days_to_sell_p75: float | None = None
    if days_to_sell_values:
        sorted_values = sorted(days_to_sell_values)
        n = len(sorted_values)

        def _percentile(p: float) -> float:
            # Linear interpolation; for n=1 returns the single value; for even-length
            # lists at p=0.5 produces the same midpoint average as the prior median
            # calculation (verified algebraically for n in {2,3,4}).
            idx = (n - 1) * p
            lo = int(idx)
            hi = min(lo + 1, n - 1)
            frac = idx - lo
            return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])

        days_to_sell_p25 = _percentile(0.25)
        days_to_sell_p50 = _percentile(0.50)
        days_to_sell_p75 = _percentile(0.75)
        # Backwards compat: median field preserves the original API shape.
        days_to_sell_median = days_to_sell_p50

    # 3. Feedback aggregation.
    feedback_result = await fetch_listing_feedback(item_id=item_id, days=90)
    feedback_summary = summarise_feedback(feedback_result["entries"])

    # 4. Cases (optional — gated by include_cases).
    cases_summary: dict[str, object] = {"open_cases": None}
    if include_cases:
        cases_result = await fetch_listing_cases(item_id=item_id, days=90)
        cases_summary = {"open_cases": cases_result["open_cases"]}

    # 5. Sell-through (uses same window for sold + unsold).
    sold_in_window = await fetch_sold_listings(days=min(window_days, 60), per_page=200)
    unsold_in_window = await fetch_unsold_listings(days=min(window_days, 60), per_page=200)
    sold_count = sum(1 for s in sold_in_window["listings"] if s["item_id"] == str(item_id))
    unsold_count = sum(1 for u in unsold_in_window["listings"] if u["item_id"] == str(item_id))
    str_pct = sell_through_rate(sold_count, unsold_count)

    # 6. Funnel ratios — Phase 1 uses listing.view_count + listing.watch_count.
    funnel = compute_funnel(
        view_count=listing["view_count"],
        watch_count=listing["watch_count"],
        quantity_sold=listing["quantity_sold"],
        question_count=listing["question_count"],
        days_on_site=listing["days_on_site"],
    )

    # 6b. Phase 2 traffic report — best-effort; fail-soft on auth/network/shape errors
    # so OAuth-unconfigured MCPs still get Phase 1 diagnosis.
    traffic_sales_conversion_pct: float | None = None
    traffic_return_rate_pct: float | None = None
    search_impression_share_pct: float | None = None
    store_impression_share_pct: float | None = None
    search_view_share_pct: float | None = None
    organic_search_exposure_pct: float | None = None
    rate_source = "default"
    phase2_available = False
    try:
        traffic = await fetch_traffic_report([str(item_id)], days=min(window_days, 90))
        summary = parse_traffic_report_response(traffic)
        if summary["records_count"] > 0:
            funnel["impressions"] = summary["impressions"]
            # Phase 2 backfill — HitCount is deprecated so Phase 1 gave us
            # view_count=None, leaving funnel["views"] and the view-dependent
            # ratios as None. Overwrite with the Analytics-API ground truth.
            funnel["views"] = summary["views"]
            funnel["ctr_pct"] = summary["ctr_pct"]
            if summary["views"] > 0:
                funnel["watchers_per_100_views"] = round(
                    100.0 * listing["watch_count"] / summary["views"], 2
                )
                funnel["conversion_rate_pct_approx"] = round(
                    100.0 * listing["quantity_sold"] / summary["views"], 2
                )
            else:
                # GENUINE ZERO — Phase 2 confirmed views=0. Use 0.0 ratios
                # (distinct from None which signals "data not available").
                funnel["watchers_per_100_views"] = 0.0
                funnel["conversion_rate_pct_approx"] = 0.0
            traffic_sales_conversion_pct = summary["sales_conversion_rate_pct"]
            search_impression_share_pct = summary["search_impression_share_pct"]
            store_impression_share_pct = summary["store_impression_share_pct"]
            search_view_share_pct = summary["search_view_share_pct"]
            organic_search_exposure_pct = summary["organic_search_exposure_pct"]
            phase2_available = True
    except Exception as e:
        # Documented fail-soft: Phase 2 enrichment is best-effort. On any failure
        # (auth, timeout, unexpected shape, network) fall back to Phase 1 diagnosis
        # rather than killing the whole analyse_listing response.
        log_debug(f"analyse_listing traffic_report_skipped reason={type(e).__name__}: {e}")

    try:
        rr = await rest_compute_return_rate(item_id=item_id, days=90)
        if rr.get("return_rate_pct") is not None:
            traffic_return_rate_pct = float(rr["return_rate_pct"])
            rate_source = "measured (Phase 2, 90d)"
    except Exception as e:
        log_debug(f"analyse_listing return_rate_skipped reason={type(e).__name__}: {e}")

    # 7. Rank health — Phase 2 feeds sales_conversion_rate_pct when live.
    # None-preserving: when Phase 2 is unavailable, watchers_per_100_views
    # may be None; absolute-signal fallback (watchers/units_sold kwargs)
    # covers that case.
    wp100 = funnel["watchers_per_100_views"]
    rank_health = compute_rank_health(
        days_on_site=listing["days_on_site"],
        watchers_per_100_views=float(wp100) if wp100 is not None else None,
        sales_conversion_rate_pct=traffic_sales_conversion_pct,
        watchers=listing["watch_count"],
        units_sold=listing["quantity_sold"],
    )

    # 8. Floor/ceiling — measured return rate (Phase 2) preferred over default.
    if traffic_return_rate_pct is not None:
        floor_result = compute_floor_price(return_rate=traffic_return_rate_pct / 100.0)
    else:
        floor_result = compute_floor_price()
    floor_gbp = float(floor_result["floor_gbp"])
    ceiling_gbp = float(floor_result["suggested_ceiling_gbp"])

    # 9. Current price + verdict.
    try:
        current_price_gbp = float(listing["price"])
    except (ValueError, TypeError):
        current_price_gbp = None
    verdict = price_verdict(
        current_price=current_price_gbp,
        floor=floor_gbp,
        return_rate=float(floor_result["inputs"]["return_rate"]),
        source=(
            f"config/fees.yaml defaults, return rate {rate_source}"
            if rate_source == "default"
            else f"measured return rate, {rate_source}"
        ),
    )

    signals: dict[str, object] = {
        "sell_through_rate_pct": str_pct,
        "days_to_sell_median": days_to_sell_median,
        "feedback_positive_pct": feedback_summary["feedback_positive_pct"],
        "dsr_item_as_described": feedback_summary["dsr_item_as_described"],
        "sales_conversion_rate_pct": traffic_sales_conversion_pct,
        "return_rate_pct": traffic_return_rate_pct,
        # Phase 1.3.4 — surface the 4 new traffic-share signals so the weekly
        # sweep + decision matrix can consume them. None when Phase 2 unavailable.
        "search_impression_share_pct": search_impression_share_pct,
        "store_impression_share_pct": store_impression_share_pct,
        "search_view_share_pct": search_view_share_pct,
        "organic_search_exposure_pct": organic_search_exposure_pct,
    }
    if include_cases:
        signals["open_cases"] = cases_summary["open_cases"]

    # 10. Diagnosis + action.
    diagnosis, action = diagnose_listing(
        funnel=funnel,
        signals=signals,
        rank_health=rank_health,
        price_gbp=current_price_gbp,
        floor_gbp=floor_gbp,
    )

    multi_qty_note = None
    if listing["quantity_sold"] > 0:
        multi_qty_note = (
            "Price/quantity revisions do not reset Cassini rank on multi-quantity "
            "listings — sales history is preserved."
        )
        if rank_health == "STABLE" and action is not None:
            action = (
                f"{action} Rank is stable — price revisions do not reset "
                "Cassini ranking on multi-quantity listings."
            )

    # G-NEW-1 (AC at #10 line 397) — when the diagnose_listing recommendation
    # surfaces "enable Best Offer", auto-suggest concrete thresholds so the
    # operator doesn't need a second `recommend_best_offer_thresholds` round-
    # trip. The action string is natural-language ("Drop price 5-8% or enable
    # Best Offer.") not an underscore-form enum, so we case-insensitive match
    # the phrase. Computed only when the recommendation triggers (cheap
    # pure-fn). Stage 5 Sonnet HIGH-4 fix: substring `"enable_best_offer"`
    # never fired because the action string spells it with spaces.
    best_offer_thresholds: dict | None = None
    if action and "best offer" in action.lower() and floor_gbp is not None:
        try:
            best_offer_thresholds = compute_best_offer_thresholds(
                floor_gbp=floor_gbp,
                live_price_gbp=current_price_gbp,
            )
        except (ValueError, TypeError) as exc:
            log_debug(
                f"analyse_listing best_offer_thresholds_skipped item_id={item_id} "
                f"reason={type(exc).__name__}: {exc}"
            )

    # Stub #19 — opt-in market-concentration computation. Adds one Browse API
    # call and one filter-pipeline run; expensive enough that we gate on the
    # caller's explicit request to avoid latency regression for read-mostly
    # consumers (e.g. dashboard polling).
    comp_pool_stats: dict | None = None
    comp_verdict: str | None = None  # Stub #20 — 3-verdict carve-out
    comp_recommended_action: str | None = None
    if include_market_concentration:
        try:
            mpns = (listing.get("specifics") or {}).get("MPN") or []
            mpn = str(mpns[0]) if mpns else None
            if mpn:
                comp_result = await fetch_competitor_prices(
                    part_number=mpn,
                    condition=listing.get("condition_name") or "USED",
                    location_country="GB",
                    own_listing=listing,
                    own_live_price=current_price_gbp,
                )
                if isinstance(comp_result, dict):
                    audit_flat = comp_result.get("audit_flat") or {}
                    comp_pool_stats = audit_flat.get("concentration")
                    # Stub #20 — surface 3-verdict carve-out + per-verdict
                    # recommended_action so analyse_listing's decision matrix
                    # consumer routes on LONE_SUPPLIER / THIN_POOL / ALL_FILTERED
                    # correctly. Verdict only present for non-normal pools.
                    comp_verdict = comp_result.get("verdict")
                    comp_recommended_action = comp_result.get("recommended_action")
        except (RuntimeError, KeyError, TypeError, ValueError) as e:
            log_debug(
                f"analyse_listing market_concentration_skipped item_id={item_id} "
                f"reason={type(e).__name__}: {e}"
            )
            comp_pool_stats = None

    response_payload = {
        "item_id": str(item_id),
        "window_days": window_days,
        "phase2_available": phase2_available,
        "funnel": funnel,
        "signals": signals,
        # #17 fix per Stage 1 L2 F2: surface days_on_site from listing_to_dict
        # (key was previously dropped from the response shape — was never a
        # regression, just a feature-add closing the decision-matrix consumer
        # gap). Value MAY be None when ListingDetails.StartTime missing/malformed
        # per listings.py:182 — legitimate, NOT an error.
        "days_on_site": listing["days_on_site"],
        # G-NEW-3: days-to-sell distribution (p25/p50/p75/n_samples) for
        # pricing-review consumer. days_to_sell_median preserved for backwards
        # compat (alias of p50).
        "days_to_sell_n_samples": days_to_sell_n_samples,
        "days_to_sell_p25": days_to_sell_p25,
        "days_to_sell_p50": days_to_sell_p50,
        "days_to_sell_p75": days_to_sell_p75,
        "days_to_sell_median": days_to_sell_median,
        "multi_qty_note": multi_qty_note,
        "rank_health_status": rank_health,
        "diagnosis": diagnosis,
        "recommended_action": action,
        "floor_price_gbp": floor_gbp,
        "suggested_ceiling_gbp": ceiling_gbp,
        "current_price_gbp": current_price_gbp,
        "price_verdict": verdict,
        # Stub #19 — only present when include_market_concentration=True; None
        # when the comp scan failed or the listing has no MPN to query.
        "comp_pool_stats": comp_pool_stats,
        # Stub #20 — 3-verdict carve-out (LONE_SUPPLIER / THIN_POOL /
        # ALL_FILTERED). Only set when the comp scan ran AND the pool was
        # non-normal. Operator decision-matrix consumer routes on these:
        #   LONE_SUPPLIER  → anchor_via_cogs_plus_target_margin
        #   THIN_POOL      → use_with_low_confidence
        #   ALL_FILTERED   → review_filter_settings
        "comp_verdict": comp_verdict,
        "comp_recommended_action": comp_recommended_action,
        # G-NEW-1 — present only when recommended_action triggers
        # 'enable_best_offer' AND a floor is computable. None otherwise.
        "best_offer_thresholds": best_offer_thresholds,
    }

    # Phase 5.2.1 — only persist when Phase 2 is actually available; without
    # traffic data the snapshot can't drive elasticity later.
    if phase2_available:
        try:
            append_snapshot(
                "analysis_baseline",
                str(item_id),
                {
                    "price_gbp": current_price_gbp,
                    "quantity": listing.get("quantity"),
                    "watch_count": listing["watch_count"],
                    "view_count": funnel.get("views"),
                    "traffic_30d": {
                        "impressions": funnel.get("impressions"),
                        "views": funnel.get("views"),
                        "ctr_pct": funnel.get("ctr_pct"),
                        "sales_conversion_rate_pct": traffic_sales_conversion_pct,
                        "search_impression_share_pct": search_impression_share_pct,
                        "store_impression_share_pct": store_impression_share_pct,
                        "search_view_share_pct": search_view_share_pct,
                        "organic_search_exposure_pct": organic_search_exposure_pct,
                    },
                    "source": "analyse_listing",
                },
            )
        except (OSError, ValueError) as e:
            log_debug(
                f"analyse_listing snapshot_append_failed item_id={item_id} "
                f"reason={type(e).__name__}: {e}"
            )

    return json.dumps(response_payload, indent=2)


@mcp.tool()
@with_error_handling
async def get_traffic_report(listing_ids: list[str], days: int = 30) -> str:
    """REST Analytics traffic report — impressions, CTR, views, sales conversion.

    Requires OAuth user-token with sell.analytics.readonly scope.

    Args:
        listing_ids: eBay item IDs.
        days: Lookback window in days (1-90). Default 30.

    Returns the parsed summary (impressions, views, transactions, ctr_pct,
    sales_conversion_rate_pct, per_listing breakdown). Uses the same shared
    parser as analyse_listing so values are consistent between the two tools.
    """
    log_debug(f"get_traffic_report ids={len(listing_ids)} days={days}")
    raw = await fetch_traffic_report(listing_ids=listing_ids, days=days)
    summary = parse_traffic_report_response(raw)
    return json.dumps(summary, indent=2)


@mcp.tool()
@with_error_handling
async def get_listing_returns(item_id: str, days: int = 90) -> str:
    """Post-Order v2 return search. READ-ONLY (never-dispute-customer rule).

    Requires OAuth user-token with sell.fulfillment.readonly scope.
    """
    log_debug(f"get_listing_returns item_id={item_id} days={days}")
    result = await fetch_listing_returns(item_id=item_id, days=days)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def compute_return_rate(item_id: str, days: int = 90) -> str:
    """Per-SKU return rate (joins sold count + returns). Phase 2 MCP tool.

    Args:
        item_id: eBay item ID.
        days: Window for both sold-count and returns (1-90). Default 90.
    """
    log_debug(f"compute_return_rate item_id={item_id} days={days}")
    result = await rest_compute_return_rate(item_id=item_id, days=days)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def compute_return_rates_bulk(item_ids: list[str], days: int = 90) -> str:
    """G-NEW-11 — bulk per-SKU return rates for the weekly-sweep skill flow.

    Loop the per-item compute_return_rate call across many item_ids in a single
    MCP invocation. Justification over shell-loop:

      1. Avoids N tool-call round-trips at the MCP wire layer
      2. Per-item failure isolation — one bad item doesn't bomb the batch
      3. Surfaces a high_return_rate_count summary inline so the operator
         doesn't have to filter the per-item dict to find the >15% outliers
      4. Systemic-failure short-circuit (L14): tracks repeat (exception class,
         message) signatures; when ≥3 items observed AND ≥50% share the same
         signature, the loop aborts so an OAuth/auth/rate-limit failure can't
         cascade through 50+ items as identical \"error\" entries.

    Note: per-item OAuth session reuse is NOT currently implemented — each
    call enters its own get_post_order_session(). Future optimisation: wrap
    a single context manager around the loop. The MCP wire-layer saving is
    the dominant win today.

    Args:
        item_ids: List of eBay item IDs.
        days: Window for both sold-count + returns (1-90). Default 90.

    Returns:
        JSON with shape:
          {
            "results": {
              "<item_id>": {"return_rate_pct": float, "sold_count": int,
                            "returned_count": int} | {"error": str, "error_class": str},
              ...
            },
            "summary": {"total": int, "processed": int, "succeeded": int,
                        "failed": int, "high_return_rate_count": int,
                        "high_return_rate_threshold_pct": 15.0,
                        # On short-circuit (≥50% identical errors across ≥3 items):
                        "short_circuited": bool,
                        "systemic_error_class": str,
                        "systemic_error_message": str,
                        "systemic_error_count": int,
                        "unprocessed_item_ids": list[str]}
          }
    """
    if not item_ids:
        return json.dumps({"error": "item_ids must be non-empty"})
    if not 1 <= days <= 90:
        return json.dumps({"error": f"days must be in [1, 90], got {days}"})

    log_debug(f"compute_return_rates_bulk item_count={len(item_ids)} days={days}")

    # Stage 5 swarm: lifted to config/fees.yaml `return_rates_bulk:` block
    # so operators can tune without code changes (no-hardcoded-settings rule).
    from ebay.fees import _load_fees_config  # noqa: PLC0415
    bulk_cfg = (_load_fees_config().get("return_rates_bulk") or {})
    short_circuit_threshold = float(bulk_cfg.get("short_circuit_signature_majority_pct", 0.5))
    short_circuit_min_observations = int(bulk_cfg.get("short_circuit_min_observations", 3))
    high_return_rate_threshold = float(bulk_cfg.get("high_return_rate_threshold_pct", 15.0))

    results: dict[str, dict] = {}
    succeeded = 0
    failed = 0
    high_return = 0
    # L14 (Ralph Opus) — track repeat exception class + message so a systemic
    # failure (auth-token expired, OAuth 401, rate-limit) doesn't cascade
    # silently across every remaining item. After we've seen
    # ≥short_circuit_min_observations calls AND ≥short_circuit_threshold of
    # those calls share the same (class, str) signature, abort the loop and
    # surface the systemic failure in the summary.
    error_signature_counts: dict[tuple[str, str], int] = {}
    short_circuited = False
    for idx, item_id in enumerate(item_ids):
        try:
            res = await rest_compute_return_rate(item_id=item_id, days=days)
            results[item_id] = res
            succeeded += 1
            rate = res.get("return_rate_pct")
            if isinstance(rate, (int, float)) and rate > high_return_rate_threshold:
                high_return += 1
        except Exception as e:  # noqa: BLE001 — boundary for per-item resilience
            log_debug(f"compute_return_rates_bulk item_id={item_id} FAILED: {e}")
            results[item_id] = {"error": str(e), "error_class": type(e).__name__}
            failed += 1
            sig = (type(e).__name__, str(e))
            error_signature_counts[sig] = error_signature_counts.get(sig, 0) + 1
            observed = idx + 1
            top_class, top_msg = max(error_signature_counts, key=error_signature_counts.get)  # type: ignore[arg-type]
            top_count = error_signature_counts[(top_class, top_msg)]
            if (
                observed >= short_circuit_min_observations
                and top_count / observed >= short_circuit_threshold
            ):
                log_debug(
                    f"compute_return_rates_bulk SHORT-CIRCUIT after {observed} items "
                    f"({top_count} {top_class!r}=='{top_msg}' — likely systemic)"
                )
                short_circuited = True
                break

    summary: dict[str, object] = {
        "total": len(item_ids),
        "processed": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "high_return_rate_count": high_return,
        "high_return_rate_threshold_pct": high_return_rate_threshold,
    }
    if short_circuited:
        top_class, top_msg = max(error_signature_counts, key=error_signature_counts.get)  # type: ignore[arg-type]
        summary["short_circuited"] = True
        summary["systemic_error_class"] = top_class
        summary["systemic_error_message"] = top_msg
        summary["systemic_error_count"] = error_signature_counts[(top_class, top_msg)]
        summary["unprocessed_item_ids"] = [
            iid for iid in item_ids if iid not in results
        ]
    return json.dumps({"results": results, "summary": summary}, indent=2)


@mcp.tool()
@with_error_handling
async def find_competitor_prices(
    part_number: str,
    condition: str = "USED",
    location_country: str = "GB",
    limit: int = 50,
) -> str:
    """Browse API market-price scan. Excludes own seller (set EBAY_OWN_SELLER_USERNAME).

    Uses app-token (client_credentials). Returns price distribution (min/p25/
    median/p75/max) + shipping/best-offer/promoted rates + listings[].

    Args:
        part_number: MPN / model number to search.
        condition: NEW | USED | USED_EXCELLENT | OPENED | FOR_PARTS.
        location_country: ISO 2-letter country code. Default GB.
        limit: Max listings to fetch (1-200). Default 50.
    """
    log_debug(
        f"find_competitor_prices pn={part_number} cond={condition} country={location_country}"
    )
    result = await fetch_competitor_prices(
        part_number=part_number,
        condition=condition,
        location_country=location_country,
        limit=limit,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def get_store_info() -> str:
    """GetStore wrapper — returns store name + custom categories.

    Uses Auth'N'Auth (Trading API). No OAuth scope required.

    Returns JSON: {store_name, store_categories: [{category_id, category_name,
    category_order}], categories_count}.

    `categories_count` is a convenience aggregate so the weekly sweep can
    flag stores with 0 custom categories (cross-promotion disabled at root,
    per Doc 14 L165 zero-store-impressions diagnosis).
    """
    log_debug("get_store_info called")
    result = await fetch_store_info()
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def revise_pictures(
    item_id: str,
    photo_paths: list[str],
    mode: str = "append",
    confirm: bool = False,
    dry_run: bool = False,
) -> str:
    """Revise the PictureURL list on a live listing — append or replace.

    Replaces the bridge-script pattern (e.g. /tmp/push_photos_<item_id>.py)
    with a first-class MCP tool. Uploads each path to eBay Picture Services,
    composes the new ordered URL list, and applies via ReviseFixedPriceItem
    with ShippingDetails echo-back. Honours the Revise-path no-Quantity
    invariant (build_revise_payload._assert_no_quantity).

    Args:
        item_id: eBay item ID.
        photo_paths: Local image paths to upload + apply.
        mode: 'append' (default) or 'replace'.
        confirm: REQUIRED when mode='replace' — the destructive path.
        dry_run: If True, return the composed URL plan without uploading or
            calling ReviseFixedPriceItem.

    Returns:
        JSON with item_id, mode, photos_before, photos_after / photos_after_preview,
        photos_lost (replace-only), truncated, truncated_count, fees, live_url.
    """
    try:
        result = await _revise_pictures_core(
            item_id=item_id,
            photo_paths=photo_paths,
            mode=mode,
            confirm=confirm,
            dry_run=dry_run,
        )
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def recommend_best_offer_thresholds(
    item_id: str,
    auto_accept_pct: float | None = None,
    auto_decline_pct: float | None = None,
) -> str:
    """G-NEW-1 — recommend Best Offer auto-accept / auto-decline thresholds.

    Pure-function helper composes thresholds from the listing's live price and
    the per-SKU floor (config/fees.yaml + measured return rate when Phase 2
    OAuth available). Standalone — call against any item_id, not gated by
    analyse_listing's recommended_action.

    Operator workflow: receive recommendation → call update_listing(
    item_id, best_offer_enabled=True, best_offer_auto_accept_gbp=auto_accept_gbp,
    best_offer_auto_decline_gbp=auto_decline_gbp) to apply.

    Issue #16: defaults shifted from 0.88/0.72 hardcoded to None so the MCP
    tool surface agrees with the autonomous responder by default — both
    inherit `config/fees.yaml best_offer:` (operator-locked 0.925/0.75).
    Operator can still override per-call by passing explicit floats. Two
    surfaces (this MCP tool + respond_best_offers.py) now return the SAME
    numbers for the SAME live price unless explicitly overridden.

    Args:
        item_id: eBay item ID.
        auto_accept_pct: Fraction of live price for auto-accept. None (default)
            inherits from config/fees.yaml best_offer.auto_accept_pct.
        auto_decline_pct: Fraction of live price for auto-decline. None (default)
            inherits from config/fees.yaml best_offer.auto_decline_pct.

    Returns:
        JSON with item_id, live_price_gbp, floor_gbp, auto_accept_gbp,
        auto_decline_gbp, return_rate_source, rationale.
    """
    if not item_id or not item_id.strip():
        return json.dumps({"error": "item_id required"})

    log_debug(f"recommend_best_offer_thresholds item_id={item_id}")

    response = await asyncio.to_thread(
        execute_with_retry,
        "GetItem",
        {
            "ItemID": item_id,
            "DetailLevel": "ReturnAll",
            "IncludeItemSpecifics": "true",
            "IncludeWatchCount": "true",
        },
    )
    if response.reply.Item is None:
        return json.dumps({"error": f"item {item_id} not found or no longer active"})

    listing = listing_to_dict(response.reply.Item)
    try:
        live_price_gbp = float(listing["price"])
    except (TypeError, ValueError):
        return json.dumps({"error": f"item {item_id} has no usable price"})

    floor_result, rate_source = await _measure_or_default_floor(item_id)
    floor_gbp = float(floor_result["floor_gbp"])

    thresholds = compute_best_offer_thresholds(
        floor_gbp=floor_gbp,
        live_price_gbp=live_price_gbp,
        auto_accept_pct=auto_accept_pct,
        auto_decline_pct=auto_decline_pct,
    )

    return json.dumps(
        {
            "item_id": item_id,
            "live_price_gbp": live_price_gbp,
            "return_rate_source": rate_source,
            **thresholds,
        },
        indent=2,
    )


@mcp.tool()
@with_error_handling
async def get_elasticity(
    item_id: str,
    before_event: str = "analysis_baseline",
    after_event: str = "post_change_check",
) -> str:
    """Return pricing elasticity for the given item from price_snapshots.jsonl.

    Reads ~/.local/share/ebay-seller-tool/price_snapshots.jsonl, finds the
    first matching before_event + after_event for item_id, computes
    Δwatchers / Δprice_pct.

    Use cases:
      - Operator post-walk-back: did the price drop move watchers?
        (expect classification=price_sensitive after a real walk-back)
      - Operator pre-raise: was the prior raise inelastic?
        (no point raising further if the previous raise didn't move demand)
      - Sweep diagnostic: which listings have actionable elasticity data?

    Defaults match Issue #14 Phase 1 wiring: analysis_baseline (written by
    analyse_listing) → post_change_check (written by update_listing on every
    real price change). Override to ("price_change", "post_change_check") to
    measure the immediate price-action delta directly.

    Args:
        item_id: eBay item ID (string).
        before_event: snapshot event_type to use as baseline. Default
            "analysis_baseline".
        after_event: snapshot event_type to use as the post-change check.
            Default "post_change_check".

    Returns:
        JSON. On success: {item_id, before_event, after_event, before_price,
        after_price, before_watchers, after_watchers, delta_price_pct,
        delta_watchers, elasticity, classification, before_timestamp,
        after_timestamp, freshness_warning?}. ``freshness_warning`` is set
        to "events_too_close" when (after_timestamp - before_timestamp) is
        below WRONG_DIRECTION_FRESHNESS_DAYS (default 7) — the elasticity
        classification may be artefact rather than settled buyer behaviour.
        Re-run after a 7+ day settled period for diagnostic confidence.

        On missing data: {error: "insufficient_events", item_id,
        before_event, after_event}.
    """
    result = await asyncio.to_thread(
        compute_elasticity, str(item_id), before_event, after_event
    )
    if result is None:
        return json.dumps(
            {
                "item_id": str(item_id),
                "error": "insufficient_events",
                "before_event": before_event,
                "after_event": after_event,
            }
        )

    # Freshness gate (AC2.4 / L2.A FN-7) — warn when before/after pair sits
    # inside the 7-day settled window. The immediate post_change_check fired
    # by update_listing happens ~3-5s after ReviseFixedPriceItem; watcher
    # counts on eBay don't shift in seconds. A classification computed on
    # this short window is a snapshot artefact, not buyer behaviour.
    before_ts = result.get("before_timestamp")
    after_ts = result.get("after_timestamp")
    if before_ts and after_ts:
        try:
            from datetime import datetime  # noqa: PLC0415

            b = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
            a = datetime.fromisoformat(after_ts.replace("Z", "+00:00"))
            delta_days = (a - b).total_seconds() / 86400.0
            result["delta_days"] = round(delta_days, 3)
            if 0 <= delta_days < 7:
                result["freshness_warning"] = "events_too_close"
                result["freshness_note"] = (
                    "Watcher counts typically do not shift in <7 days; "
                    "classification may read 'inelastic' as a snapshot artefact. "
                    "Re-run after 7+ day settled period for diagnostic confidence."
                )
        except (ValueError, AttributeError) as e:
            log_debug(
                f"get_elasticity timestamp_parse_failed item_id={item_id} "
                f"reason={type(e).__name__}: {e}"
            )

    return json.dumps(result, indent=2)


@mcp.tool()
@with_error_handling
async def end_listing(
    item_id: str,
    expected_title: str,
    ending_reason: str = "NotAvailable",
    confirm: bool = False,
    dry_run: bool = True,
) -> str:
    """End a single live eBay listing via Trading API EndFixedPriceItem.

    Destructive — once ended, the original ItemID is gone (relisting requires
    a NEW ItemID). Defaults to dry_run=True; caller must opt in to the live
    path with dry_run=False AND confirm=True.

    Single-item by design — no bulk loop. The expected_title echo-back guard
    catches "wrong ItemID" mistakes before any side-effect: caller must quote
    the live title (case-insensitive substring match).

    Args:
        item_id: eBay item ID.
        expected_title: caller's claimed title for this listing — checked
            case-insensitively as substring match. Mismatch refuses loudly.
        ending_reason: one of NotAvailable / LostOrBroken / Incorrect /
            OtherListingError / SellToHighBidder. Defaults to NotAvailable
            (out-of-stock — most common case).
        confirm: REQUIRED True on the live path. Defaults False.
        dry_run: True (default) returns a preview without API side-effects.

    Returns:
        JSON: dry_run preview shape OR live shape with ack + end_time.
    """
    log_debug(
        f"end_listing item_id={item_id} reason={ending_reason} "
        f"dry_run={dry_run} confirm={confirm}"
    )
    try:
        result = await _end_listing_core(
            item_id=item_id,
            expected_title=expected_title,
            ending_reason=ending_reason,
            confirm=confirm,
            dry_run=dry_run,
        )
    except ValueError as e:
        return json.dumps(
            {
                "error": str(e),
                "allowed_ending_reasons": list(ALLOWED_ENDING_REASONS),
            },
            indent=2,
        )
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    log_debug("Starting ebay-seller-tool MCP server")
    mcp.run()
