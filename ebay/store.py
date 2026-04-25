"""
Trading API GetStore wrapper (Issue #13 Phase 1.5).

Read-only — surfaces store name + custom categories so the weekly sweep
can detect store-level signals (e.g. zero categories = no cross-promotion).

Auth: Auth'N'Auth user-token (same as the rest of selling/listings) —
no OAuth scope required.

Rate limits: GetStore is on the Trading-API shared bucket (5,000/day).
"""

from __future__ import annotations

import asyncio
from typing import Any

from ebay.client import execute_with_retry, log_debug


def _as_list(node: Any) -> list:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _sync_get_store_info() -> dict[str, Any]:
    """Call GetStore and reshape into a stable dict.

    Returns:
        {
            "store_name": str | None,
            "store_categories": [
                {"category_id": str, "category_name": str, "category_order": int | None},
                ...
            ],
            "categories_count": int,
        }

    `categories_count` is a convenience aggregate so the weekly sweep can
    flag stores with 0 custom categories (cross-promotion disabled at root).
    """
    response = execute_with_retry(
        "GetStore",
        {"CategoryStructureOnly": "false"},
    )
    store = getattr(response.reply, "Store", None)
    if store is None:
        return {"store_name": None, "store_categories": [], "categories_count": 0}

    store_name = getattr(store, "Name", None)
    custom = getattr(store, "CustomCategories", None)
    raw_cats = []
    if custom is not None:
        raw_cats = _as_list(getattr(custom, "CustomCategory", None))

    categories: list[dict[str, Any]] = []
    for cat in raw_cats:
        cat_id_raw = getattr(cat, "CategoryID", None)
        cat_name_raw = getattr(cat, "Name", None)
        cat_order_raw = getattr(cat, "Order", None)
        order: int | None = None
        if cat_order_raw is not None:
            try:
                order = int(cat_order_raw)
            except (TypeError, ValueError):
                order = None
        categories.append(
            {
                "category_id": str(cat_id_raw) if cat_id_raw is not None else None,
                "category_name": str(cat_name_raw) if cat_name_raw is not None else None,
                "category_order": order,
            }
        )

    result = {
        "store_name": str(store_name) if store_name is not None else None,
        "store_categories": categories,
        "categories_count": len(categories),
    }
    # AP #12 observability — log outcome (input is logged at server.py call site).
    log_debug(
        f"get_store_info result store_name={result['store_name']!r} "
        f"categories_count={result['categories_count']}"
    )
    return result


async def fetch_store_info() -> dict[str, Any]:
    """Async wrapper around GetStore — same shape as _sync_get_store_info."""
    return await asyncio.to_thread(_sync_get_store_info)
