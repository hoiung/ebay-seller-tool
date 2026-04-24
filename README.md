# ebay-seller-tool

MCP server for managing eBay listings from Claude Code. Built to solve real seller problems: bulk listing updates, automated description generation, photo uploads, and inventory management.

## What

A Python MCP (Model Context Protocol) server that connects Claude Code directly to eBay's APIs. Instead of manually copy-pasting titles, descriptions, and item specifics into eBay's web UI, this server lets you create, update, and manage listings through natural language in your terminal.

## Why

Built for a side hustle selling on eBay. Managing listings manually is slow and error-prone. When you need to update descriptions across all your active listings, doing it one by one through eBay's UI takes hours. With this MCP server, it's one command.

## Features

- **Create listings** from structured data (title, HTML description, item specifics, photos)
- **Bulk update descriptions** across all active listings (add warnings, fix text, update specs)
- **Upload photos** directly from local filesystem to eBay Picture Services
- **Get active listings** with current stats (price, quantity, views, watchers)
- **Update inventory** quantities and pricing
- **Smart templates** with Jinja2 for consistent listing HTML (warnings, condition badges, spec tables)

## Tech Stack

- Python 3.11+ with FastMCP
- eBay Trading API (XML) for listing CRUD
- eBay REST APIs for inventory management
- Jinja2 for HTML template rendering
- Pillow for photo processing
- uv for dependency management

## Setup

### Prerequisites

1. [eBay Developer Account](https://developer.ebay.com/join) (free)
2. Production API keyset from Developer Portal
3. Auth'N'Auth token from eBay account settings
4. Python 3.11+, uv

### Install

```bash
git clone https://github.com/hoiung/ebay-seller-tool.git
cd ebay-seller-tool
cp .env.example .env
# Fill in your eBay credentials in .env
uv sync
```

### OAuth Setup (Phase 2/3 analytics tools)

Analytics + Post-Order + Browse tools need OAuth. Trading-API tools only need `EBAY_AUTH_TOKEN`.

```bash
# One-time consent for user-token (Analytics + Post-Order returns)
uv run python scripts/oauth_setup.py
# Browser opens; approve; paste the redirect URL when prompted.
# Writes EBAY_OAUTH_REFRESH_TOKEN to .env.
```

`.env` keys (added for Phase 2-4): `EBAY_APP_CLIENT_ID`, `EBAY_APP_CLIENT_SECRET`, `EBAY_OAUTH_RU_NAME`, `EBAY_OAUTH_REFRESH_TOKEN`, `EBAY_OWN_SELLER_USERNAME`.

### Register with Claude Code

```bash
claude mcp add ebay-seller-tool \
  -- uv --directory /path/to/ebay-seller-tool run python server.py
```

### Test

```bash
# MCP Inspector (browser-based tool tester)
uv run mcp dev server.py

# Or use directly from Claude Code
claude
> get my active eBay listings
```

## MCP Tools

| Tool | Status | Description |
|---|---|---|
| `get_active_listings` | Implemented | List all active listings with stats |
| `get_listing_details` | Implemented | Full details for a single listing |
| `update_listing` | Implemented | Revise title, description, price, condition, item specifics (quantity blocked). **Phase 4**: refuses to revise below computed floor price. |
| `upload_photos` | Implemented | Upload local photos to eBay Picture Services |
| `create_listing` | Implemented | Create a new fixed-price eBay listing end-to-end |
| `get_sold_listings` / `get_unsold_listings` | Implemented (#4 Phase 1) | GetMyeBaySelling SoldList/UnsoldList wrappers |
| `get_seller_transactions` | Implemented (#4 Phase 1) | GetSellerTransactions with derived days-to-sell |
| `get_listing_feedback` | Implemented (#4 Phase 1) | Per-transaction feedback + DSR aggregate |
| `get_listing_cases` | Implemented (#4 Phase 1) | Resolution cases (EBP_INR + EBP_SNAD) — read-only |
| `floor_price` | Implemented (#4 Phase 1) | Break-even price under return-risk scenarios |
| `analyse_listing` | Implemented (#4 Phase 1) | Funnel + signals + diagnosis + floor/ceiling |
| `get_traffic_report` | Implemented (#4 Phase 2) | REST Analytics: impressions, CTR, sales conversion |
| `get_listing_returns` / `compute_return_rate` | Implemented (#4 Phase 2) | Post-Order v2 return search + per-SKU rate |
| `find_competitor_prices` | Implemented (#4 Phase 3) | Browse API market scan with own-seller exclusion |

### Usage

**Upload photos** (returns ordered eBay-hosted URLs):
```python
upload_photos(
    photo_paths=["/path/to/IMG20260420090000.jpg", "/path/to/IMG20260420090001.jpg"],
    dry_run=False,
)
```

**Create listing** (end-to-end from a product folder — default `dry_run=True`):
```python
# Dry-run first — uses VerifyAddFixedPriceItem, no live listing created
create_listing(
    folder_path="/path/to/Hard Disks/2_5_inch/ST2000NX0253",
    price=49.99,
    quantity=1,
    condition="Used",          # {New, Opened, Used, Used - Excellent}
    has_caddy=False,
    dry_run=True,              # default
)

# Apply — real AddFixedPriceItem. UUID-idempotent: same folder = same UUID.
create_listing(
    folder_path="/path/to/Hard Disks/2_5_inch/ST2000NX0253",
    price=49.99,
    quantity=1,
    condition="Used",
    has_caddy=False,
    dry_run=False,
)
```

## Project Structure

```
ebay-seller-tool/
├── server.py              # MCP server entrypoint
├── ebay/                  # eBay API client layer
│   ├── client.py          # Trading API connection factory
│   ├── listing.py         # Create/revise/end listing logic
│   ├── inventory.py       # Quantity and bulk operations
│   ├── photos.py          # Photo upload and processing
│   └── conditions.py      # Condition name to eBay ID mapping
├── business/              # Business rules (private, loaded at runtime)
│   ├── title_generator.py # Title builder with configurable rules
│   ├── warning_rules.py   # Compatibility warning engine
│   └── part_lookup.py     # Part number lookup integration
├── templates/             # Jinja2 HTML templates
│   ├── base.html          # Base listing HTML shell
│   └── warnings/          # Warning block templates
├── scripts/               # Standalone utilities
│   ├── auth_setup.py      # Initial OAuth/token setup
│   └── export_listings.py # Dump listings to JSON
└── docs/
    └── research/          # Decision logs and API research
```

## Development Workflow

This project uses [SST3-AI-Harness](https://github.com/hoiung/SST3-AI-Harness) for all development. SST3 is a 5-stage autonomous AI workflow (Research, Issue Creation, Triple-Check, Implementation, Post-Implementation Review) with mandatory quality gates, multi-tier code review (Ralph Review), and enforcement via pre-commit hooks. Every change follows the same process: issue-driven, branch-per-issue, verified before merge.

## Research

See [docs/research/](docs/research/) for the full decision log on why we built this instead of using existing MCP servers, API evaluation, and architecture decisions.

## Context

Built for a personal eBay side hustle. The listing workflow includes business-specific rules for title generation, compatibility warnings, and item specifics management. Product details and business strategies are kept private.

## License

MIT
