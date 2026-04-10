# eBay API, MCP Server & ebay-seller-tool Repo Architecture

**Research date:** 2026-04-10
**Purpose:** Evaluate options for managing eBay listings programmatically from Claude Code, and architect a dedicated `ebay-seller-tool` repo.
**Decision:** Build a custom Python MCP server using FastMCP + Trading API. Migrate listing workflow from manual HTML copy-paste to direct API push.

---

## 1. eBay Developer Program

### Signup

- **Free.** No registration fee, no paid tiers.
- Register at https://developer.ebay.com/join
- Accept the eBay API License Agreement
- Generate Sandbox + Production keysets in the Developer Portal

### Before Production Works

You must subscribe to or opt out of **Marketplace Account Deletion/Closure Notifications** before Production keyset is usable. If you don't store eBay user data, request an exemption. See: https://developer.ebay.com/marketplace-account-deletion

### Keyset Structure

Each environment (Sandbox/Production) gives you:
- **App ID / Client ID** (public identifier)
- **Dev ID** (developer account ID, shared across apps)
- **Cert ID / Client Secret** (secret, used in token requests)

---

## 2. API Types: REST vs Trading API

### Decision: Use Trading API (XML) for listings, REST for inventory management

| API | Type | Use Case | Status |
|---|---|---|---|
| **Trading API** | XML, Legacy | Create/revise/end listings with full HTML descriptions (CDATA) | Stable, selective deprecation |
| **Inventory API** | REST | SKU-based inventory, bulk price/quantity updates, offers | Current, recommended by eBay |
| **Fulfillment API** | REST | Orders, shipping, cancellations | Current |
| **Account API** | REST | Payment/return/shipping policies | Current |
| **Commerce Media API** | REST | Photo upload | Current |
| **Taxonomy API** | REST | Category lookup | Current |

### Critical Rule: Do Not Mix APIs on the Same Listing

If you create via Inventory API, you MUST revise via Inventory API. Trading API ReviseItem will not work on Inventory-created listings, and vice versa. The models are incompatible (SKU-based vs ItemID-based).

### Why Trading API for Listings

- Supports CDATA HTML descriptions directly (our listing HTMLs work as-is)
- Battle-tested for create/revise/end flows
- Auth is simpler (Auth'N'Auth token, 18-month lifetime)
- `ebaysdk` Python library handles it cleanly
- REST Inventory API is better for quantity/price management but awkward for HTML descriptions

### Trading API Deprecation Notes

- Finding API + Shopping API: decommissioned Feb 2025
- `ExtendSiteHostedPictures`: decommissioned July 28, 2025
- `GetCategoryFeatures`: deprecated, decomm May 4, 2026
- Core Trading calls (AddFixedPriceItem, ReviseFixedPriceItem, GetMyeBaySelling, UploadSiteHostedPictures): no announced deprecation
- Full status: https://developer.ebay.com/develop/get-started/api-deprecation-status

---

## 3. Authentication

### For Trading API: Auth'N'Auth Token

- Simplest path. Get from eBay account Settings > Application Access, or via GetSessionID/FetchToken flow.
- Token lasts **18 months**. Store securely.
- `ebaysdk` Python library accepts as `EBAY_AUTH_TOKEN` env var.

### For REST APIs: OAuth 2.0

| Token | Grant Flow | Lifetime | Use |
|---|---|---|---|
| Application Token | Client Credentials | 2 hours | Public/read-only calls |
| User Token | Authorization Code | 2 hours | Selling operations |
| Refresh Token | Issued with User Token | 18 months | Renew User Tokens without re-auth |

### Required OAuth Scopes (if using REST)

- `https://api.ebay.com/oauth/api_scope/sell.inventory`
- `https://api.ebay.com/oauth/api_scope/sell.listing`
- `https://api.ebay.com/oauth/api_scope/sell.account`

### Official OAuth Python Client

https://github.com/eBay/ebay-oauth-python-client

---

## 4. eBay UK Specifics (EBAY_GB)

| Setting | Value |
|---|---|
| Marketplace ID | `EBAY_GB` |
| Trading API Site ID | `3` |
| Currency | GBP |
| VAT | 20% standard, included in price |
| Header (REST) | `X-EBAY-C-MARKETPLACE-ID: EBAY_GB` |
| Payment | eBay Managed Payments only |

### Category IDs

Use Taxonomy API: `GET https://api.ebay.com/commerce/taxonomy/v1/get_default_category_tree_id?marketplace_id=EBAY_GB`

UK sellers must populate item specifics fully for tech categories. Incomplete specifics cause listing warnings or search suppression.

---

## 5. Rate Limits

- **Default:** 5,000 API calls/day for new developer accounts
- Per-resource limits (not single global counter)
- Bulk endpoints (25 items/call) dramatically reduce call count
- 5,000/day is more than adequate for our use case (~22 active listings)
- Increase via Application Growth Check (free) if needed
- Check limits: `GET /developer/analytics/v1_beta/rate_limit/`
- HTTP 429 on breach

---

## 6. Sandbox vs Production

| Aspect | Sandbox | Production |
|---|---|---|
| Base URL | `api.sandbox.ebay.com` | `api.ebay.com` |
| Keys | Separate | Separate |
| Money | Play money | Real GBP |
| Listings | Not visible on real eBay | Live |
| Stability | Known quirks, periodic breakage | Stable |

Recommendation: test core flows in Sandbox, do a single-listing smoke test in Production before bulk operations.

---

## 7. Fees

- **API access: Free.** No per-call charges, no monthly subscription.
- **Seller fees (separate):**
  - Insertion: first 1,000/month free
  - Final Value Fee: ~12.8% + 30p per order
  - Store subscription optional (Basic 27/mo)
  - VAT on eBay fees for UK sellers

---

## 8. Existing eBay MCP Servers

### Tier 1: Worth Evaluating

| Server | Stars | Tools | Maintained | Verdict |
|---|---|---|---|---|
| **YosefHayim/ebay-mcp** | 44 | 325 (all Sell APIs) | Yes (April 2026, 616 commits, 958 tests) | Best community option. Claude Code supported. |
| **eBay/npm-public-api-mcp** | 4 | 4 (meta/discovery) | Official but 1 commit, very early | 2-hour token expiry with manual refresh. Not for automation. |
| **kingl0w/ebay-mcp-read-write** | 0 | 7 (listing CRUD) | Unknown | Requires Cloudflare R2 for images. Scoped but untested. |

### Tier 2: Limited / Read-Only

- `jlsookiki/secondhand-mcp` (8 stars): multi-marketplace search, buyer-side only
- `zapthedingbat/ebay-search-mcp-server`: UK-focused search, no seller tools
- `hanku4u/ebay-mcp-server`: Python, deal hunting, no listings
- `CooKey-Monster/EbayMcpServer`: single auction search tool

### Decision: Build Custom

Reasons:
1. `YosefHayim/ebay-mcp` has 325 tools but they're generic wrappers. We need custom business logic: title generation, warning injection, part number handling, condition-specific templates.
2. Our listing workflow has specific rules that a generic server can't encode.
3. Photo management from local filesystem needs custom handling.
4. A dedicated repo lets us version-control listing templates alongside the MCP server.
5. Python + FastMCP matches our existing MCP server pattern.

---

## 9. Architecture: ebay-seller-tool Repo

### Tech Stack

- **Python 3.11+** with `uv` package manager
- **FastMCP** (`mcp[cli]`) for MCP server
- **ebaysdk** for Trading API (create/revise listings, upload photos)
- **ebay-rest** for REST Inventory API (quantity management)
- **httpx** for direct REST calls
- **Jinja2** for HTML template rendering
- **Pillow** for photo resize/compress before upload

### Repo Structure

```
ebay-seller-tool/
├── .env                          # Credentials (gitignored)
├── .gitignore
├── CLAUDE.md                     # Project-specific Claude instructions
├── pyproject.toml
├── uv.lock
│
├── server.py                     # MCP server entrypoint (FastMCP, tool defs)
│
├── ebay/
│   ├── __init__.py
│   ├── client.py                 # Trading API connection factory, token mgmt
│   ├── listing.py                # create/revise/end listing logic
│   ├── inventory.py              # quantity management, bulk ops
│   ├── photos.py                 # upload, resize/compress
│   └── conditions.py             # condition name to eBay ID mapping
│
├── business/
│   ├── title_generator.py        # Title builder with configurable rules
│   ├── warning_rules.py          # Compatibility warning engine
│   ├── part_lookup.py            # Part number lookup integration
│   └── pricing.py                # Price suggestions (optional)
│
├── templates/
│   ├── base.html                 # Base HTML shell
│   ├── listing_description.html  # Jinja2 listing template
│   ├── warnings/                 # Warning block templates
│   └── item_specifics/
│       └── fields.py             # Required/optional fields per category
│
├── photos/                       # Photo processing utilities
│   └── processor.py              # Resize, compress before upload
│
├── scripts/
│   ├── auth_setup.py             # OAuth2 / Auth'N'Auth initial setup
│   ├── bulk_add_warning.py       # Add warning to all active listings
│   ├── migrate_listings.py       # Import existing HTML listings
│   └── export_listings.py        # Dump active listings to JSON/CSV
│
└── docs/
    ├── ARCHITECTURE.md
    └── EBAY_API_SETUP.md         # Dev account setup walkthrough
```

### MCP Tools (6 core)

| Tool | Purpose | eBay API |
|---|---|---|
| `create_listing` | Create new fixed-price listing from structured data | Trading: AddFixedPriceItem |
| `update_listing` | Revise title, description, price, quantity | Trading: ReviseFixedPriceItem |
| `bulk_update_descriptions` | Add/replace HTML in multiple listings at once | Trading: GetItem + ReviseFixedPriceItem |
| `get_active_listings` | List all active listings with stats | Trading: GetMyeBaySelling |
| `update_inventory_quantity` | Update stock quantity | Trading: ReviseFixedPriceItem |
| `upload_photos` | Upload local JPGs to eBay Picture Services | Trading: UploadSiteHostedPictures |

### Claude Code Registration

```bash
claude mcp add ebay-seller-tool \
  -e EBAY_APP_ID=xxx \
  -e EBAY_CERT_ID=xxx \
  -e EBAY_DEV_ID=xxx \
  -e EBAY_AUTH_TOKEN=xxx \
  -e EBAY_SITE_ID=3 \
  -- uv --directory /path/to/ebay-seller-tool run python server.py
```

### pyproject.toml

```toml
[project]
name = "ebay-seller-tool-mcp"
version = "0.1.0"
description = "MCP server for eBay seller listing management"
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.9.0",
    "ebaysdk>=2.2.0",
    "ebay-rest>=1.1.4",
    "httpx>=0.27.0",
    "pillow>=10.0.0",
    "jinja2>=3.1.0",
    "python-dotenv>=1.0.0",
]
```

---

## 10. Migration Path

### Phase 1: Foundation
1. Register eBay Developer account (free, 5 min)
2. Generate Production keyset
3. Get Auth'N'Auth token from eBay account
4. Create `ebay-seller-tool` repo with basic MCP server
5. Implement `get_active_listings` tool (read-only, safe to test)
6. Verify tool works from Claude Code

### Phase 2: Read + Write
7. Implement `upload_photos` tool
8. Implement `create_listing` tool
9. Test with one listing in Sandbox
10. Test with one real listing in Production
11. Implement `update_listing` tool

### Phase 3: Bulk Operations
12. Implement `bulk_update_descriptions` (today's warning update = one tool call)
13. Implement `update_inventory_quantity`
14. Migrate existing HTML templates to Jinja2

### Phase 4: Full Workflow
15. Integrate title_generator and warning_rules into create_listing
16. Part number lookup integration
17. Photo pipeline (local filesystem > resize > upload > listing)
18. Update listing skill to use MCP tools instead of writing HTML files

---

## 11. What This Solves

| Current Pain | With MCP Server |
|---|---|
| Create HTML file, open in browser, copy-paste title + description + item specifics into eBay manually | `create_listing` tool pushes directly to eBay |
| Bulk update 22 listings = edit 22 HTML files + copy-paste each one | `bulk_update_descriptions` updates all 22 in one tool call |
| Check what's live = log into eBay Seller Hub | `get_active_listings` shows everything in Claude Code |
| Upload photos = drag-and-drop in eBay manually | `upload_photos` from local filesystem |
| Price changes = eBay UI per listing | `update_listing` or `update_inventory_quantity` |

---

## Key URLs

| Resource | URL |
|---|---|
| Developer Portal | https://developer.ebay.com |
| Join / Register | https://developer.ebay.com/join |
| Inventory API docs | https://developer.ebay.com/api-docs/sell/inventory/overview.html |
| Trading API docs | https://developer.ebay.com/Devzone/XML/docs/Reference/eBay/index.html |
| OAuth guide | https://developer.ebay.com/api-docs/static/oauth-tokens.html |
| Rate limits | https://developer.ebay.com/develop/get-started/api-call-limits |
| Sandbox | https://developer.ebay.com/develop/tools/sandbox |
| Account deletion compliance | https://developer.ebay.com/marketplace-account-deletion |
| API deprecation status | https://developer.ebay.com/develop/get-started/api-deprecation-status |
| eBay OAuth Python client | https://github.com/eBay/ebay-oauth-python-client |
| YosefHayim/ebay-mcp | https://github.com/YosefHayim/ebay-mcp |

---

## Sources

- eBay Developer Program documentation (developer.ebay.com)
- eBay Inventory API Overview and Methods reference
- eBay Trading API Reference (XML)
- eBay OAuth2 Token and Refresh Token guides
- GitHub: YosefHayim/ebay-mcp, eBay/npm-public-api-mcp, kingl0w/ebay-mcp-read-write
- MCP specification (modelcontextprotocol.io)
- Claude Code MCP documentation (code.claude.com/docs/en/mcp)
