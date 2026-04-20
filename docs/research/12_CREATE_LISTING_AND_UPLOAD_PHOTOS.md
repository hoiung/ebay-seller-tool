# Research — `create_listing` + `upload_photos` MCP tools

**Research date**: 2026-04-20
**Status**: Design complete; implementation pending.
**Purpose**: Close the "photo-first listing" gap in the eBay seller workflow — extend the MCP server with two tools so that a listing can be created end-to-end from local drive-label photos + a handful of commercial inputs, without manual copy-paste into eBay's web UI.

---

## 1. Findings

### 1.1 Existing MCP tool pattern to mirror (`update_listing` as reference)

- **Decorator stack** — `@mcp.tool()` + `@with_error_handling` (`server.py:45-62`). The wrapper catches all non-SystemExit exceptions, logs `traceback.format_exc()` via `log_debug`, returns `json.dumps({"error", "tool", "details"}, indent=2)`.
- **Function shape** — `async def` returning `str` (JSON). Inputs are primitives + dicts; `dry_run: bool = False` is a built-in parameter on `update_listing` (`server.py:200-209`).
- **Flow inside `update_listing`** (`server.py:265-369`):
  1. Input validation (title ≤80 chars, price > 0, `ConditionID in {1000, 1500, 3000}`, description contains `<` and `>`)
  2. `asyncio.to_thread(execute_with_retry, "GetItem", {...})` — fresh fetch for TOCTOU safety
  3. `snapshot_listing(resp.reply.Item)` — before state
  4. `compute_diff(...)` — what would change
  5. If `dry_run`: return `{"dry_run": True, "item_id", "diff"}`
  6. `extract_shipping_details(resp.reply.Item)` — echo back shipping
  7. `build_revise_payload(...)` — construct XML dict
  8. `execute_with_retry("ReviseFixedPriceItem", payload)`
  9. Second `GetItem` to verify, snapshot "after"
  10. `audit_log_write(...)` — append JSON line to `~/.local/share/ebay-seller-tool/audit.log`
  11. Return `{"success": True, "item_id", "fields_updated", "before", "after"}`

`create_listing` must follow the exact same skeleton. Only the middle (build_payload + verb) differs.

### 1.2 Existing payload helpers (reusable for `create_listing`)

- `build_revise_payload` (`ebay/listings.py:216-283`) — mirrors most of what Add needs EXCEPT: it calls `_assert_no_quantity(payload)` (`listings.py:286-296`) which forbids Quantity recursively. This safety invariant is the opposite of what Add needs. Do NOT relax it on Revise; create a separate `build_add_payload` with its own invariant (`_assert_requires_quantity(payload, min=1)`).
- `cdata_wrap` (`listings.py:208-213`) — reusable as-is for Description HTML on Add.
- `listing_to_dict` (`listings.py:18-86`) — reusable for the `before/after` snapshots.
- `extract_shipping_details` (`listings.py:147-205`) — **does NOT emit `GlobalShipping`**. For Add, either extend this function (optional param) or build ShippingDetails inline in `build_add_payload`.
- No `build_add_payload` or any `Add*` stub currently exists in `listings.py` — greenfield implementation.

### 1.3 `AddFixedPriceItem` — required fields for UK HDD category 56083

Minimum `Item` payload (verified against eBay API reference, ebaysdk-python samples, davidtsadler PHP SDK examples):

| Field | Value / Source | Note |
|---|---|---|
| `Title` | From listing HTML copy-block | ≤80 chars |
| `Description` | From listing HTML body | CDATA-wrapped |
| `PrimaryCategory.CategoryID` | `56083` | Fixed for HDDs UK |
| `StartPrice` | Caller-provided £ | String, `_currencyID="GBP"` |
| `Country` | `GB` | Fixed |
| `Currency` | `GBP` | Fixed |
| `ConditionID` | 1000 / 1500 / 3000 | Caller-provided |
| `DispatchTimeMax` | `3` | Fixed (3 business days) |
| `ListingDuration` | `GTC` | Fixed (Good 'Til Cancelled) |
| `ListingType` | `FixedPriceItem` | Fixed |
| `PaymentMethods` | (empty) | Managed Payments — left empty |
| `PictureDetails.PictureURL` | List of eBay-hosted URLs | Max 24, index 0 = gallery |
| `PostalCode` | Seller postcode | Per-account |
| `Location` | Seller location | Per-account |
| `Quantity` | Caller-provided int ≥ 1 | **REQUIRED for Add** (unlike Revise) |
| `ReturnPolicy` | `ReturnsNotAccepted` + `InternationalReturnsAcceptedOption=ReturnsNotAccepted` | Per-account policy |
| `ShippingDetails` | Flat/free + `GlobalShipping=true` | See §1.5 |
| `ItemSpecifics` | Full 21-field canonical set | **Brand + MPN REQUIRED for cat 56083** (error 21919303 if missing) |
| `SKU` | Optional, recommended | MPN-derived |
| `UUID` | 32-char hex | **REQUIRED for idempotency** — see §1.8 |

**Response** — `AddFixedPriceItemResponse.ItemID` = new listing ID. Verify via `GetItem`.

**Top validation errors** (HDD listings):
- `21919303` — missing Brand/MPN ItemSpecifics
- `21916750` — invalid category-required specifics
- `21917078` — invalid ConditionID
- `21919144` — rate-limit on Add
- `21916664` — invalid PictureURL

**Promoted Listings note** — NOT a field in `AddFixedPriceItem`. Post-create enrolment happens via separate Marketing API. A new listing will NOT auto-enrol unless an Account-level "Auto Ads" rule is active. Check Seller Hub → Marketing → Campaigns to verify nothing matches cat 56083.

**Verify dry-run** — `VerifyAddFixedPriceItem` exists. Identical payload. Returns `ItemID=0` + same `Errors` container as real Add + estimated `Fees`. Shares app-level daily quota (5,000/day). This is the production dry-run path.

### 1.4 `UploadSiteHostedPictures` — binary photo upload

- **Wire format** — HTTP `multipart/form-data`. ebaysdk supports it natively: `api.execute("UploadSiteHostedPictures", {...}, files={"file": (...)})`. **But `ebay/client.py::execute_with_retry` does NOT currently pass `files=` through** — needs patching (see §1.8).
- **One picture per call** — confirmed in eBay KB 1063. Batch = sequential calls.
- **Response URL** — `SiteHostedPictureDetails.FullURL`. Use this for `PictureDetails.PictureURL[]` on Add.
- **Limits** — max 12 MB file size, min 500 px, max 9000×9000 pixels. Stored ceiling ~1600×1600. eBay auto-downscales larger images but rejects oversized files hard.
- **Formats** — JPEG, PNG, GIF, TIFF, BMP, WEBP, HEIC, AVIF accepted. XCF NOT supported.
- **PictureSet** — `Standard` (≤400×400) vs `Supersize` (≤800×800, enables zoom). `Supersize` recommended for enterprise products where buyers want detail.
- **Hosting** — ~90 days by default, extendable via `ExtensionInDays`. Covers active listing lifetime.
- **Ordering** — XML list order determines display order. `PictureURL[0]` = gallery photo.
- **URL total length** — must be <3975 chars across all PictureURL entries.

### 1.5 ShippingDetails + ReturnPolicy + Location

Inline contract (no Business Policies opt-in assumed):

**ShippingDetails** (UK Royal Mail 2nd Class + GSP):
```xml
<ShippingDetails>
  <ShippingType>Flat</ShippingType>
  <ShippingServiceOptions>
    <ShippingServicePriority>1</ShippingServicePriority>
    <ShippingService>UK_RoyalMailSecondClassStandard</ShippingService>
    <ShippingServiceCost>0.00</ShippingServiceCost>
    <FreeShipping>true</FreeShipping>
  </ShippingServiceOptions>
  <GlobalShipping>true</GlobalShipping>
</ShippingDetails>
```
`CalculatedShippingRate` NOT required (Flat-rate listings). No explicit `InternationalShippingServiceOption` needed — GSP handles it.

**ReturnPolicy**:
```xml
<ReturnPolicy>
  <ReturnsAcceptedOption>ReturnsNotAccepted</ReturnsAcceptedOption>
  <InternationalReturnsAcceptedOption>ReturnsNotAccepted</InternationalReturnsAcceptedOption>
</ReturnPolicy>
```
OMIT `ReturnsDescription` (DE/ES/IT only), `RefundOption` (deprecated), `ReturnsWithinOption` (N/A when NotAccepted).

**Location fields** — Item-level: `Country`, `Location`, `PostalCode`. `Site=UK` is a Trading connection header (`siteid=3`), NOT inside `<Item>`.

### 1.6 Photo corpus expectations

Typical product folder layout for an actively-listed HDD SKU:
- ~4 phone-camera label photos (naming convention: `IMG{YYYYMMDD}{HHMMSS}.jpg`, 2.5–5.7 MB raw Android JPEG)
- 0–2 `*.png` screenshots (CrystalDiskInfo health reports — NOT product shots)
- 1–2 `listing-{condition}.html` files (local review source)
- Optional `tests/` subfolder holding disk health test records

**`upload_photos` whitelist rule**:
- **ACCEPT**: `IMG*.jpg` (phone-camera photos — authoritative per workflow)
- **SKIP**: `*.png` (CDI screenshots), `*.xcf` (GIMP source), `listing*.html`, `tests/`, supplier-render filenames like `<PN>.jpg` or `<PN>-N.jpg` (may show wrong variant)

**Non-HDD categories** (NIC, SSD) have a different photo convention dominated by supplier renders + XCF. `create_listing` is HDD-only for now; separate tools will be needed per category.

### 1.7 Field extraction from a single label photo

A single drive-label photo reliably yields ~17 of 21 item specifics via vision model:
- OEM manufacturer + model number (from label + photo filename convention)
- HPE model, HPE P/N, HPE GPN (if HPE-branded)
- Firmware, DOM, country of origin, serial number
- Capacity, RPM, interface (text on label)
- Transfer Rate (derived from interface speed)
- Form Factor (from folder path)
- EAN, Manufacturer Warranty, Unit Type (fixed defaults)

**NOT on label — must be caller-provided or looked up**:
- **Price** — caller-provided
- **Quantity** — caller-provided
- **Condition** — caller-provided (`New` / `Opened` / `Used`)
- **Caddy present?** — sometimes visually inferrable; require explicit flag for reliability
- **Cache size** — rarely printed; lookup via `ebay/hdd_specs.py` catalogue
- **Product Line / Series** — often suppressed on HPE-branded labels; lookup required
- **Height** (15 mm for 2.5") — not printed; lookup by MPN
- **Compatible With**, **Features** — category marketing defaults
- **Colour** — fixed category default (e.g. `Silver`)

Recommendation: build `ebay/hdd_specs.py` as an MPN → `{cache, series, height, family_label}` lookup table, seeded from the current live-listings corpus. Enriched each time a new MPN is processed.

### 1.8 Safety, idempotency, rollback

Critical gap in `execute_with_retry` (`ebay/client.py:72-160`): **no idempotency guard**. If `AddFixedPriceItem` succeeds server-side but the response is lost (network drop), retry creates a duplicate listing. Three layers of defence:

1. **UUID in request payload** — `Item.UUID` is a 32-char hex string (0-9, A-F, no dashes). eBay remembers recent UUIDs for "a few days" (exact window not documented). Replay of same UUID returns `DuplicateInvocationDetails` with the original `ItemID` — no duplicate created. MUST generate per-call UUID in `build_add_payload` and keep it stable across retries.
2. **Pre-flight `VerifyAddFixedPriceItem`** — validates payload, returns fees + errors, no listing created.
3. **Post-create verify** — `GetItem(new_item_id)` to confirm the listing went live as expected.

**Rollback** — if verify shows wrong data, call `EndFixedPriceItem(ItemID, EndingReason="OtherListingError")`. Fee refunded if zero sales.

**Fees (UK cat 56083, 2026)** — 1,000 free insertions per month. Typical HDD-seller volumes are deep within quota.

**Duplicate-listings policy** — enforced post-hoc by eBay's Cassini title-similarity check, not at Add-time. Mitigation: `GetMyeBaySelling` search by title/MPN before calling Add, to catch client-side accidental duplicates.

### 1.9 Client layer patches required

Two structural changes to `ebay/client.py`:

1. **`execute_with_retry` must accept `files=None` kwarg** and pass to `api.execute(verb, data, files=files)` — required for `UploadSiteHostedPictures` multipart.
2. **Optional UUID-aware retry** — same UUID on retry (not a new one per attempt). Either: generate UUID inside `build_add_payload`, persist to a retry-state dict keyed by item_id, OR accept UUID as input to `execute_with_retry` for Add specifically.

### 1.10 Image preprocessing (`upload_photos` internals)

Canonical Pillow pipeline:

```python
from io import BytesIO
from PIL import Image, ImageOps

EBAY_MAX = (1600, 1600)  # stored ceiling; downscale client-side to save bandwidth
JPEG_QUALITY = 90

def preprocess_for_ebay(path: str) -> bytes:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)     # bake rotation into pixels
        im = im.convert("RGB")                # strip alpha, 8-bit RGB
        im.thumbnail(EBAY_MAX, Image.LANCZOS) # aspect-preserving, never upscales
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        # omitting `exif=` strips GPS/phone metadata
        return buf.getvalue()
```

- **HEIC** — optional `pillow-heif` add-on; `register_heif_opener()` at import time. Wrapped in try/except so the dep is optional.
- **XCF** — not supported; `upload_photos` must refuse with a clear error ("Export to JPG from GIMP: File → Export As").
- **Cost** — ~400-900 ms per 4000×3000 JPEG on modern x86, ~36 MB memory peak. Serial upload is fine for 8-12 photos per listing.
- **Ordering** — `upload_photos` must accept explicit ordered path list. Filesystem timestamps are unreliable as gallery-photo selectors.

### 1.11 Infrastructure already in place

- `pyproject.toml` has `pillow>=10.0.0`, `httpx>=0.27.0`, `jinja2>=3.1.0` — all the deps are there.
- `photos/` directory is empty (stub for upload staging).
- `templates/item_specifics/`, `templates/warnings/` are empty `.gitkeep` placeholders — Jinja2 isn't actually used yet. **No existing description-HTML builder** — `create_listing` must either accept ready HTML from the caller or build one from a new Jinja2 template.
- `business/__init__.py` is 0 bytes. Business rules live in a separate private repo.
- Auth is Auth'N'Auth token (18-month static lifetime) with `GetTokenStatus` check on startup and <30-day warning (`auth.py:37-75`). No auto-refresh.

---

## 2. Gaps identified

| # | Gap | Impact | Mitigation |
|---|---|---|---|
| G1 | Category 56083 authoritative required-aspects list not fetched live | May be missing eBay-required ItemSpecifics | Call `GetCategorySpecifics(CategoryID=56083, SiteID=3)` once at build time, cache the result, compare against the 21-field canonical set |
| G2 | Exact UUID dedup window duration not documented by eBay | If retry happens beyond window, duplicate could still be created | Keep retry attempts short (current 15s deadline is well inside "a few days") |
| G3 | Raw phone JPG vs cleaned artwork decision | Raw phone photos are 2.5-5.7 MB with EXIF and no cropping | Resolve by preprocessing (auto-crop, resize, re-encode, strip EXIF). Phone photo is the authoritative source; cleaned artwork optional via flag |
| G4 | MPN → cache/series/height lookup table source | Without lookup, tool can't fully auto-populate non-label fields | Build `ebay/hdd_specs.py` seeded from existing live listings |
| G5 | Caddy P/N extraction from photo unreliable | Storage Format accuracy depends on caddy identification | Require explicit `has_caddy` parameter; optionally accept `caddy_photo_path` |
| G6 | Promoted Listings auto-enrolment detection | New listing could be swept into an existing Auto-Ads rule | After create, call Marketing API to verify non-enrolment |
| G7 | NIC / SSD product categories outside HDD scope | `create_listing` is HDD-only | Defer; add separate tools per category when needed |
| G8 | Sandbox app keys not configured | Can't test end-to-end without creating real listings | Get sandbox keys from eBay Developer Portal; add `EBAY_SANDBOX=true` flag; use `VerifyAddFixedPriceItem` as primary dry-run until then |

---

## 3. Plan / design

### 3.1 Module layout

```
ebay-seller-tool/
├── server.py                      # + 2 new @mcp.tool() functions
├── ebay/
│   ├── client.py                  # PATCH: add files= kwarg to execute_with_retry
│   ├── listings.py                # + build_add_payload + _assert_requires_quantity
│   ├── photos.py                  # NEW — preprocess + upload flow
│   ├── hdd_specs.py               # NEW — MPN → {cache, series, height, family} lookup
│   └── ...
├── templates/
│   └── listing_description.html   # NEW — Jinja2 template for description body
├── photos/                        # staging dir — upload inputs + preprocessed outputs
```

### 3.2 `create_listing` MCP tool signature

```python
@mcp.tool()
@with_error_handling
async def create_listing(
    folder_path: str,                       # product folder under EBAY_DRIVE_ROOT
    price: float,                           # GBP
    quantity: int,                          # >= 1
    condition: str,                         # "New" | "Opened" | "Used" | "Used - Excellent"
    has_caddy: bool,                        # affects Storage Format + title
    photo_paths: list[str] | None = None,   # explicit order; default = glob IMG*.jpg
    description_html: str | None = None,    # if None, render from template
    dry_run: bool = True,                   # VerifyAddFixedPriceItem
    picture_urls: list[str] | None = None,  # if already uploaded, skip upload step
) -> str:
```

Flow (mirrors `update_listing`):
1. Validate inputs (price > 0, quantity ≥ 1, condition in valid set, folder exists)
2. Read product folder: find `IMG*.jpg` for vision extraction + `listing-{condition}.html`
3. Extract label facts from photos (vision call or pre-extracted manifest)
4. Build ItemSpecifics (21 canonical fields) from extraction + `hdd_specs.py` lookup + defaults
5. Build ShippingDetails + ReturnPolicy + PictureDetails + rest of payload
6. Generate `UUID` and include in payload
7. **Pre-flight `VerifyAddFixedPriceItem`** — if errors, return them without applying
8. If `dry_run`: return `Fees`, `Errors`, and full computed payload preview
9. If `picture_urls is None`: call `upload_photos` first to populate
10. `execute_with_retry("AddFixedPriceItem", payload)` — with the SAME UUID on retry
11. `GetItem(new_item_id)` — verify what landed
12. Check Promoted Listings not enrolled (Marketing API call) — warn if enrolled
13. `audit_log_write(...)` — append JSON line
14. Return `{success, item_id, listing_url, fees, before: None, after: snapshot}`

### 3.3 `upload_photos` MCP tool signature

```python
@mcp.tool()
@with_error_handling
async def upload_photos(
    photo_paths: list[str],         # explicit order; first = gallery
    picture_set: str = "Supersize", # "Standard" | "Supersize"
    preprocess: bool = True,        # resize + strip EXIF via Pillow
    dry_run: bool = False,
) -> str:
```

Flow:
1. Validate inputs (≤24 photos, all paths exist, total URL-string length pre-check)
2. For each path: reject XCF with clear error; HEIC → convert via pillow-heif
3. If `preprocess`: run `preprocess_for_ebay(path)` → JPEG bytes ≤1600×1600 q90
4. If `dry_run`: return list of `{path, size_after, would_upload: True}` — no API calls
5. Sequential `execute_with_retry("UploadSiteHostedPictures", {...}, files={"file": (name, bytes)})` — **needs client.py patch**
6. Extract `SiteHostedPictureDetails.FullURL` from each response
7. Verify URL-string total < 3975 chars
8. Return `{success, urls: [...], total_chars, warnings: []}`

### 3.4 Client patch (`ebay/client.py`)

Minimal change:
```python
def execute_with_retry(verb: str, data: dict, max_attempts: int = 3, files: dict | None = None) -> object:
    # ... existing retry logic ...
    response = api.execute(verb, data, files=files) if files else api.execute(verb, data)
```

Plus optional idempotency-aware wrapper for Add — caller supplies UUID:
```python
def execute_add_with_idempotency(verb: str, data: dict, uuid_hex: str, max_attempts: int = 3) -> object:
    assert "Item" in data and data["Item"].get("UUID") == uuid_hex
    return execute_with_retry(verb, data, max_attempts=max_attempts)
```

### 3.5 `hdd_specs.py` — MPN catalogue

Structure:
```python
HDD_SPECS: dict[str, dict] = {
    "<OEM_MODEL>": {
        "brand": "<Manufacturer>",
        "family": "<Product Line>",
        "family_label_printed": <bool>,
        "capacity": "<e.g. 2TB>",
        "rpm": "<e.g. 7200 RPM>",
        "interface": "<SATA III | SAS | ...>",
        "transfer_rate": "<6G | 12G | 3G>",
        "cache": "<e.g. 128 MB>",
        "form_factor": "<2.5 in | 3.5 in>",
        "height": "<15mm | 9.5mm | null for 3.5>",
    },
    # ...
}
```

Keyed by OEM model (with `-EXOS` suffix for Seagate series variants). Provides the non-label fields (`cache`, `family`, `height`). Seed from existing live-listings corpus; extend per new MPN.

### 3.6 Jinja2 description template

`templates/listing_description.html` — driven by rendered variables `{title, warnings_html, overview, specs_table, hpe_part_numbers, condition_notes, what_included, closing}`. Produces the HTML body that currently lives in per-product `listing-{condition}.html` files.

### 3.7 Safety invariants

New: `_assert_requires_quantity(payload, min=1)` mirrors existing `_assert_no_quantity` but opposite — refuses to build if Quantity missing or < min.

Kept: existing `_assert_no_quantity` stays on Revise path — NEVER relaxed.

---

## 4. References

### eBay Trading API (URLs)

- AddFixedPriceItem — https://developer.ebay.com/devzone/xml/docs/reference/ebay/AddFixedPriceItem.html
- AddFixedPriceItemResponseType — https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/AddFixedPriceItemResponseType.html
- VerifyAddFixedPriceItem — https://developer.ebay.com/devzone/xml/docs/reference/ebay/VerifyAddFixedPriceItem.html
- EndFixedPriceItem — https://developer.ebay.com/devzone/xml/docs/reference/ebay/endfixedpriceitem.html
- UploadSiteHostedPictures — https://developer.ebay.com/devzone/xml/docs/reference/ebay/uploadsitehostedpictures.html
- ShippingDetailsType — https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ShippingDetailsType.html
- ReturnPolicyType — https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ReturnPolicyType.html
- PictureDetailsType — https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/picturedetailstype.html
- ItemType — https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/ItemType.html
- UUIDType — https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/uuidtype.html
- DuplicateInvocationDetailsType — https://developer.ebay.com/devzone/xml/docs/reference/ebay/types/DuplicateInvocationDetailsType.html
- Global Shipping Program — https://developer.ebay.com/api-docs/user-guides/static/trading-user-guide/global-shipping-specify.html
- Picture hosting — https://developer.ebay.com/api-docs/user-guides/static/trading-user-guide/picture-hosting.html
- Item location — https://developer.ebay.com/api-docs/user-guides/static/trading-user-guide/location.html
- Business Policies — https://developer.ebay.com/devzone/business-policies/Concepts/BusinessPoliciesAPIGuide.html
- Duplicate Listings Policy — https://www.ebay.com/help/policies/listing-policies/duplicate-listings-policy?id=4255
- KB 482 (shipping block contiguity) — https://developer.ebay.com/support/kb-article?KBid=482
- KB 1063 (EPS FAQ) — https://ebaydts.com/eBayKBDetails?KBid=1063
- KB 1240 (multi-picture) — https://developer.ebay.com/support/kb-article?KBid=1240
- KB 2062 (image sizes) — https://developer.ebay.com/support/kb-article?KBid=2062
- KB 52 (UUID idempotency) — https://developer.ebay.com/support/kb-article?KBid=52

### Python tooling

- ebaysdk samples — https://github.com/timotheus/ebaysdk-python/blob/master/samples/trading.py
- davidtsadler AddFixedPriceItem example — https://github.com/davidtsadler/ebay-sdk-examples/blob/master/trading/04-add-fixed-price-item.php
- pillow-heif PyPI — https://pypi.org/project/pillow-heif/
- Pillow performance — https://python-pillow.github.io/pillow-perf/

### Code provenance (this repo)

- `server.py` — MCP tool pattern (lines 45-62, 67-369)
- `ebay/listings.py` — payload builders (lines 18-296)
- `ebay/client.py` — retry wrapper (lines 23-160)
- `ebay/auth.py` — Auth'N'Auth + GetTokenStatus (lines 9-75)
- `pyproject.toml` — deps (lines 6-13)
- `docs/research/11_EBAY_API_AND_MCP_SERVER.md` — prior design decisions
