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

| Tool | Description |
|---|---|
| `create_listing` | Create a new fixed-price eBay listing |
| `update_listing` | Revise title, description, price, or quantity |
| `bulk_update_descriptions` | Add/replace HTML across multiple listings |
| `get_active_listings` | List all active listings with stats |
| `update_inventory_quantity` | Update stock quantity for a listing |
| `upload_photos` | Upload local photos to eBay Picture Services |

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
