"""
ebay-seller-tool MCP server.

Provides tools for managing eBay listings from Claude Code.
Uses eBay Trading API (XML) for listing CRUD and photo uploads.
"""

import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ebay-seller-tool")


def log_debug(msg: str) -> None:
    print(f"[ebay-seller-tool] {msg}", file=sys.stderr, flush=True)


# Tools will be added here as they are implemented.
# See docs/research/11_EBAY_API_AND_MCP_SERVER.md for the full tool plan.


if __name__ == "__main__":
    log_debug("Starting ebay-seller-tool MCP server")
    mcp.run()
