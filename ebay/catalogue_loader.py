"""Generic fail-loud runtime loader for the private listing data set.

The PUBLIC ebay-seller-tool ships ZERO product/category data. At runtime the
catalogue (per-model spec rows), the listing-contract (ItemSpecifics field
schema + constant values + transfer-rate rules + category id), and the
competitor-recognition taxonomy overlay are loaded from a private data
directory pointed at by ``EBAY_LISTING_DATA_DIR``.

Modelled on the private ``_hpe_aliases.py`` reader: env-override + 3-layer
fail-loud schema validation. The loader NEVER silently downgrades a missing /
empty / malformed private file to an empty catalogue — that would let the tool
emit a blank or wrong listing. Fail loud, always.

Layers of fail-loud (mirrored by ``tests/test_catalogue_loader.py``):
  1. env unset                  -> raise ``ListingDataError``
  2. data file missing          -> raise, echoing the resolved path
  3. file empty / wrong schema  -> raise; never return ``{}``

This module is the single private-data access layer. It also hosts the merged
public+private filter-config overlay (``load_filter_config``) and the shared
reset seam (``reset_caches``) that ``ebay.browse`` and ``ebay.title_benchmark``
both route through, so there is one source of truth for reading private data.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_ENV_DIR = "EBAY_LISTING_DATA_DIR"

CATALOGUE_FILENAME = "hdd-specs.yaml"
CONTRACT_FILENAME = "listing-contract.yaml"
TAXONOMY_FILENAME = "series-taxonomy.yaml"

CATALOGUE_SCHEMA = "listing-catalogue-v1"
CONTRACT_SCHEMA = "listing-contract-v1"
TAXONOMY_SCHEMA = "series-taxonomy-v1"

# Reset-hook registry (AC 2.2 shared seam). Consumers that keep their OWN
# process-local derived caches on top of the loader (``ebay.browse``'s compiled
# regex caches, ``ebay.title_benchmark``'s sorted-config cache) register a
# clear-callback here at import time. ``reset_caches()`` invokes them all, so a
# single call to the shared seam invalidates EVERY listing-data-derived cache —
# without this low-level loader importing its higher-level consumers (no layering
# inversion; each module keeps ownership of its own cache-clearing logic).
_RESET_HOOKS: list[Callable[[], None]] = []


def register_reset_hook(hook: Callable[[], None]) -> None:
    """Register a consumer cache-clear callback invoked by :func:`reset_caches`."""
    if hook not in _RESET_HOOKS:
        _RESET_HOOKS.append(hook)


# The public generic config lives in the repo; the private overlay is merged
# onto it at runtime (Phase 2). This path is category-AGNOSTIC — it holds only
# generic knobs (filler words, quality thresholds, condition equivalence).
_PUBLIC_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing_and_content.yaml"

# Required sub-keys every catalogue row must carry. The SHAPE is generic (this
# is a public contract); the VALUES are private. ``height`` may be ``None``
# (e.g. for form factors with no short/tall variant).
_REQUIRED_CATALOGUE_KEYS = (
    "brand",
    "family",
    "capacity",
    "rpm",
    "interface",
    "transfer_rate",
    "cache",
    "form_factor",
    "height",
)

# Required top-level keys in the listing-contract.
_REQUIRED_CONTRACT_KEYS = (
    "category_id",
    "required_spec_fields",
    "transfer_rate",
    "storage_format",
    "item_specifics",
)


class ListingDataError(Exception):
    """Raised when the private listing data dir is unset/missing/malformed.

    Callers MUST distinguish "data unavailable, abort loudly" from any silent
    fallback — there is no silent fallback. A blank or partial listing is worse
    than a hard failure the operator can see and fix.
    """


def _data_dir() -> Path:
    raw = os.environ.get("EBAY_LISTING_DATA_DIR")  # _ENV_DIR — EBAY_* convention
    if not raw:
        raise ListingDataError(
            f"{_ENV_DIR} is not set. The public repo ships no product data; "
            f"point {_ENV_DIR} at the private listing-data directory "
            "(see .env.example), or at ebay/listing_data.example for the "
            "public test suite. Refusing to run with an empty catalogue."
        )
    return Path(raw)


def _load_yaml_mapping(path: Path, expected_schema: str) -> dict[str, Any]:
    """Load a YAML mapping and enforce the ``schema:`` sentinel (fail-loud)."""
    if not path.exists():
        raise ListingDataError(f"listing data file not found: {path}")
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ListingDataError(f"malformed YAML at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ListingDataError(
            f"schema: top-level of {path} must be a mapping (got {type(data).__name__})"
        )
    if data.get("schema") != expected_schema:
        raise ListingDataError(
            f"schema: {path} expected {expected_schema!r}, got {data.get('schema')!r}"
        )
    return data


@lru_cache(maxsize=1)
def _load_catalogue() -> dict[str, dict[str, str | None]]:
    data = _load_yaml_mapping(_data_dir() / CATALOGUE_FILENAME, CATALOGUE_SCHEMA)
    rows = data.get("catalogue")
    if not isinstance(rows, dict) or not rows:
        raise ListingDataError(
            f"schema: 'catalogue' in {CATALOGUE_FILENAME} must be a non-empty mapping"
        )
    for key, row in rows.items():
        if not isinstance(row, dict):
            raise ListingDataError(f"schema: catalogue[{key!r}] must be a mapping")
        for required in _REQUIRED_CATALOGUE_KEYS:
            if required not in row:
                raise ListingDataError(
                    f"schema: catalogue[{key!r}] missing required sub-key {required!r}"
                )
    return rows


@lru_cache(maxsize=1)
def _load_contract() -> dict[str, Any]:
    data = _load_yaml_mapping(_data_dir() / CONTRACT_FILENAME, CONTRACT_SCHEMA)
    for required in _REQUIRED_CONTRACT_KEYS:
        if required not in data:
            raise ListingDataError(f"schema: {CONTRACT_FILENAME} missing required key {required!r}")
    specifics = data["item_specifics"]
    if not isinstance(specifics, list) or not specifics:
        raise ListingDataError("schema: contract 'item_specifics' must be a non-empty list")
    for idx, field in enumerate(specifics):
        if not isinstance(field, dict) or "name" not in field or "source" not in field:
            raise ListingDataError(
                f"schema: contract item_specifics[{idx}] must be a mapping with 'name' and 'source'"
            )
    transfer_rate = data["transfer_rate"]
    if not isinstance(transfer_rate, dict) or "default" not in transfer_rate:
        raise ListingDataError(
            "schema: contract 'transfer_rate' must be a mapping with a 'default'"
        )
    storage_format = data["storage_format"]
    if (
        not isinstance(storage_format, dict)
        or not {
            "with_caddy",
            "without_caddy",
        }
        <= storage_format.keys()
    ):
        raise ListingDataError(
            "schema: contract 'storage_format' must define 'with_caddy' and 'without_caddy'"
        )
    return data


def load_listing_data() -> dict[str, Any]:
    """Return ``{'catalogue': {...}, 'contract': {...}}`` from the private dir.

    Fails loud (raises :class:`ListingDataError`) when the env is unset, a file
    is missing, or a file is empty / malformed / wrong-schema. NEVER returns an
    empty catalogue silently.
    """
    return {"catalogue": _load_catalogue(), "contract": _load_contract()}


# --------------------------------------------------------------------------- #
# Filter-config overlay (Phase 2)                                             #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _load_public_config() -> dict[str, Any]:
    """Load the public generic config (category-agnostic knobs only).

    Honours the ``EBAY_FILTER_CONFIG`` override so tests can swap the PUBLIC
    base deterministically — exactly as ``browse._load_filter_config`` did
    before the overlay merge. The private taxonomy overlay is read separately
    from ``EBAY_LISTING_DATA_DIR`` (see :func:`_load_taxonomy_overlay`); a test
    that needs custom series/taxonomy swaps the overlay, NOT this base.
    """
    path = Path(os.environ.get("EBAY_FILTER_CONFIG") or _PUBLIC_CONFIG_PATH)
    if not path.exists():
        raise ListingDataError(
            f"public config not found at {path}; the generic "
            "filler_words / quality / condition-equivalence knobs live here."
        )
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def _load_taxonomy_overlay() -> dict[str, Any]:
    """Load the private competitor-recognition taxonomy overlay.

    Fail-loud contract (AC 2.5) — distinguish two cases that a naive
    ``cfg.get(key, []) or []`` silently conflates:

      * ``EBAY_LISTING_DATA_DIR`` UNSET -> documented generic-only mode; return
        ``{}`` so the public tool runs with NO series/taxonomy discrimination
        (the operator accepts this when running without the private overlay).
      * ``EBAY_LISTING_DATA_DIR`` SET but the overlay file is missing / empty /
        wrong-schema -> RAISE. An overlay that was *meant* to load but did not
        must never degrade comp-filter to silently-permissive (a comp in one
        series wrongly matched against an own-listing in a different series).
    """
    raw = os.environ.get("EBAY_LISTING_DATA_DIR")  # _ENV_DIR — EBAY_* convention
    if not raw:
        return {}
    data = _load_yaml_mapping(Path(raw) / TAXONOMY_FILENAME, TAXONOMY_SCHEMA)
    overlay = data.get("taxonomy")
    if not isinstance(overlay, dict) or not overlay:
        raise ListingDataError(
            f"schema: 'taxonomy' in {TAXONOMY_FILENAME} must be a non-empty "
            f"mapping ({_ENV_DIR} is set — refusing to silently run permissive)"
        )
    return overlay


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto a deep copy of ``base``.

    Nested mappings merge key-by-key (so an overlay's
    ``comp_filter.hard_reject_patterns`` is added alongside the public
    ``broken_or_parts`` / ``bundle`` subsets rather than replacing the block).
    Non-mapping values (lists, scalars) are replaced wholesale by the overlay.
    """
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


@lru_cache(maxsize=1)
def load_filter_config() -> dict[str, Any]:
    """Return the public generic config with the private taxonomy overlay merged.

    The single source of truth for both ``ebay.browse._load_filter_config`` and
    ``ebay.title_benchmark._load_pricing_and_content_config``. Generic-only mode
    (no overlay) returns the public config unchanged; with the overlay env set,
    the private taxonomy (series_names, preserved_phrases, drive-class,
    caddy/storage patterns, sibling_allowlist) is merged in, and a missing /
    empty overlay fails loud per :func:`_load_taxonomy_overlay`.
    """
    return _deep_merge(_load_public_config(), _load_taxonomy_overlay())


def reset_caches() -> None:
    """Shared reset seam (AC 0.5 + AC 2.2).

    Invalidates EVERY cache that reads private listing data so tests can swap
    the example data / overlay deterministically: the loader's own ``lru_cache``
    layers PLUS every consumer-registered process-local cache (``ebay.browse``'s
    compiled-pattern caches, ``ebay.title_benchmark``'s sorted-config cache) via
    the reset-hook registry. ``ebay.browse`` and ``ebay.title_benchmark`` both
    route their resets through this one function, so a single call here fully
    invalidates BOTH consumers (not just the loader layer).
    """
    _load_catalogue.cache_clear()
    _load_contract.cache_clear()
    _load_public_config.cache_clear()
    _load_taxonomy_overlay.cache_clear()
    load_filter_config.cache_clear()
    for hook in _RESET_HOOKS:
        hook()
