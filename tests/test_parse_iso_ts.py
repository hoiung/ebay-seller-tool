"""Tests for ebay.listings.parse_iso_ts — the ebaysdk timestamp coercer.

Covers #40 AC2.5: `_parse_iso_ts` was promoted to the public name `parse_iso_ts`
(it was being imported across modules under its private name — the R-NAME2
smell). A newly-public callable with branching coercion logic needs a direct
test seam (AC4.12 new-public-surface self-check); previously it was only
exercised indirectly via ebay.selling integration tests.
"""

from __future__ import annotations

import datetime

from ebay.listings import parse_iso_ts


def test_none_returns_none() -> None:
    assert parse_iso_ts(None) is None


def test_empty_string_returns_none() -> None:
    assert parse_iso_ts("") is None


def test_z_suffix_passthrough() -> None:
    # Already ISO-8601 Z — returned unchanged.
    assert parse_iso_ts("2026-03-24T19:12:19Z") == "2026-03-24T19:12:19Z"


def test_utc_offset_converted_to_z() -> None:
    # eBay transmits UTC; +00:00 offset is normalised to the 'Z' suffix.
    assert parse_iso_ts("2026-03-24T19:12:19+00:00") == "2026-03-24T19:12:19Z"


def test_space_separator_normalised_to_t_and_z_appended() -> None:
    # ebaysdk datetime str() yields a naive 'YYYY-MM-DD HH:MM:SS' form; the
    # space separator becomes 'T' and 'Z' is appended (UTC made explicit).
    assert parse_iso_ts("2026-03-24 19:12:19") == "2026-03-24T19:12:19Z"


def test_datetime_object_is_coerced_via_str() -> None:
    # The real ebaysdk input: a naive datetime object (str()-coerced).
    dt = datetime.datetime(2026, 3, 24, 19, 12, 19)
    assert parse_iso_ts(dt) == "2026-03-24T19:12:19Z"


def test_only_first_space_replaced() -> None:
    # replace(" ", "T", 1) — only the date/time separator is touched, so a
    # value with a trailing token keeps its later spaces intact.
    assert parse_iso_ts("2026-03-24 19:12:19 PST") == "2026-03-24T19:12:19 PSTZ"
