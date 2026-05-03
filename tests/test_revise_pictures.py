"""Tests for ebay.pictures.revise_pictures + the server.revise_pictures MCP wrapper (#23).

Covers:
- mode validation: 'append' vs 'replace', destructive-confirm gate.
- Empty photo_paths refused.
- Item-not-found refused with clear ValueError.
- Dry-run shape: photos_before, photos_after_preview, photos_lost, no API
  side effects (no upload, no Revise call).
- Append composition: current + new in order.
- Replace composition: just new, photos_lost mirrors photos_before.
- 24-photo cap: warn-and-truncate, truncated_count surfaced, no silent drop.
- ShippingDetails echo-back: extract_shipping_details called with the live
  Item before payload build.
- _assert_no_quantity invariant (Revise path) preserved end-to-end.
- MCP wrapper: ValueError -> JSON error envelope.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import server
from ebay.pictures import revise_pictures


def _run(coro):
    return asyncio.run(coro)


def _fake_get_item(photos: list[str] | None = None) -> SimpleNamespace:
    """Build a minimal GetItem response with N existing PictureURLs."""
    if photos is None:
        photos = []
    pic_details = SimpleNamespace(PictureURL=list(photos)) if photos else None
    return SimpleNamespace(
        reply=SimpleNamespace(
            Item=SimpleNamespace(
                ItemID="123456789012",
                Title="t",
                SellingStatus=SimpleNamespace(
                    CurrentPrice=SimpleNamespace(value="10.00", _currencyID="GBP"),
                    QuantitySold="0",
                ),
                Quantity="1",
                QuantityAvailable="1",
                ListingDetails=SimpleNamespace(
                    ViewItemURL="https://www.ebay.co.uk/itm/123456789012",
                    StartTime="2026-04-01T10:00:00Z",
                    EndTime="2026-05-01T10:00:00Z",
                    RelistCount="0",
                ),
                BestOfferEnabled="false",
                BestOfferCount="0",
                QuestionCount="0",
                WatchCount="0",
                HitCount="0",
                ConditionID="3000",
                ConditionDisplayName="Used",
                PrimaryCategory=SimpleNamespace(
                    CategoryID="56083", CategoryName="Internal Hard Disk Drives"
                ),
                Description="desc",
                ShippingDetails=SimpleNamespace(
                    ShippingType="Flat",
                    ShippingServiceOptions=SimpleNamespace(
                        ShippingService="UK_RoyalMailSecondClassStandard",
                        FreeShipping="true",
                    ),
                ),
                ReturnPolicy=None,
                PictureDetails=pic_details,
                ItemSpecifics=SimpleNamespace(
                    NameValueList=[SimpleNamespace(Name="Brand", Value="Seagate")]
                ),
            )
        )
    )


def _fake_revise_response() -> SimpleNamespace:
    return SimpleNamespace(reply=SimpleNamespace(Fees=None))


# ---------- Validation gates ----------


def test_revise_pictures_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError, match=r"mode must be 'append' or 'replace'"):
        _run(revise_pictures(item_id="999", photo_paths=["/tmp/a.jpg"], mode="overwrite"))


def test_revise_pictures_replace_without_confirm_refused() -> None:
    with pytest.raises(ValueError, match=r"requires confirm=True"):
        _run(revise_pictures(item_id="999", photo_paths=["/tmp/a.jpg"], mode="replace"))


def test_revise_pictures_empty_photo_paths_refused() -> None:
    with pytest.raises(ValueError, match=r"at least 1 path"):
        _run(revise_pictures(item_id="999", photo_paths=[], mode="append"))


def test_revise_pictures_item_not_found_refused() -> None:
    not_found = SimpleNamespace(reply=SimpleNamespace(Item=None))
    with patch("ebay.pictures.execute_with_retry", side_effect=[not_found]):
        with pytest.raises(ValueError, match=r"not found or no longer active"):
            _run(revise_pictures(item_id="999", photo_paths=["/tmp/a.jpg"], dry_run=True))


# ---------- Dry-run path ----------


def test_revise_pictures_dry_run_append_no_side_effects() -> None:
    existing = ["https://eps/a.jpg", "https://eps/b.jpg"]
    with (
        patch(
            "ebay.pictures.execute_with_retry",
            side_effect=[_fake_get_item(existing)],
        ) as mock_exec,
        patch("ebay.pictures.upload_one") as mock_upload,
        patch("ebay.pictures.preprocess_for_ebay") as mock_pre,
    ):
        result = _run(
            revise_pictures(
                item_id="123",
                photo_paths=["/tmp/c.jpg"],
                mode="append",
                dry_run=True,
            )
        )
    # No upload calls in dry-run.
    assert mock_upload.call_count == 0
    assert mock_pre.call_count == 0
    # Only GetItem; no Revise call.
    assert mock_exec.call_count == 1
    assert mock_exec.call_args_list[0].args[0] == "GetItem"
    # Shape.
    assert result["dry_run"] is True
    assert result["mode"] == "append"
    assert result["photos_before"] == existing
    assert result["photos_after_preview"][:2] == existing  # append preserves order
    assert result["photos_lost"] == []
    assert result["truncated"] is False


def test_revise_pictures_dry_run_replace_shows_lost_photos() -> None:
    existing = ["https://eps/a.jpg", "https://eps/b.jpg"]
    with patch(
        "ebay.pictures.execute_with_retry",
        side_effect=[_fake_get_item(existing)],
    ):
        result = _run(
            revise_pictures(
                item_id="123",
                photo_paths=["/tmp/new.jpg"],
                mode="replace",
                confirm=True,
                dry_run=True,
            )
        )
    assert result["mode"] == "replace"
    assert result["photos_lost"] == existing
    # photos_after_preview only contains the new placeholder; the previous
    # urls are NOT carried.
    assert all("a.jpg" not in u and "b.jpg" not in u for u in result["photos_after_preview"])


# ---------- Live (mocked) path ----------


def test_revise_pictures_append_calls_revise_with_composed_urls() -> None:
    existing = ["https://eps/a.jpg"]
    new_url = "https://eps/uploaded.jpg"
    revise_payloads: list[dict] = []

    def _exec_side_effect(verb, payload, *args, **kwargs):
        if verb == "GetItem":
            return _fake_get_item(existing)
        if verb == "ReviseFixedPriceItem":
            revise_payloads.append(payload)
            return _fake_revise_response()
        raise AssertionError(f"unexpected verb {verb}")

    with (
        patch("ebay.pictures.execute_with_retry", side_effect=_exec_side_effect),
        patch("ebay.pictures.preprocess_for_ebay", return_value=b"x"),
        patch("ebay.pictures.upload_one", return_value=new_url),
    ):
        result = _run(revise_pictures(item_id="123", photo_paths=["/tmp/c.jpg"], mode="append"))

    assert result["ok"] is True
    assert result["mode"] == "append"
    assert result["photos_before"] == existing
    assert result["photos_after"] == existing + [new_url]
    assert result["photos_count_after"] == 2

    # ReviseFixedPriceItem was called once with the composed URL list.
    assert len(revise_payloads) == 1
    item = revise_payloads[0]["Item"]
    assert item["PictureDetails"]["PictureURL"] == existing + [new_url]
    # Revise-path Quantity invariant: NO quantity at any nesting level.
    assert "Quantity" not in item


def test_revise_pictures_replace_overwrites_url_list() -> None:
    existing = ["https://eps/a.jpg", "https://eps/b.jpg"]
    new_url = "https://eps/replaced.jpg"
    captured = {}

    def _exec_side_effect(verb, payload, *args, **kwargs):
        if verb == "GetItem":
            return _fake_get_item(existing)
        if verb == "ReviseFixedPriceItem":
            captured["payload"] = payload
            return _fake_revise_response()
        raise AssertionError(f"unexpected verb {verb}")

    with (
        patch("ebay.pictures.execute_with_retry", side_effect=_exec_side_effect),
        patch("ebay.pictures.preprocess_for_ebay", return_value=b"x"),
        patch("ebay.pictures.upload_one", return_value=new_url),
    ):
        result = _run(
            revise_pictures(
                item_id="123",
                photo_paths=["/tmp/new.jpg"],
                mode="replace",
                confirm=True,
            )
        )

    assert result["mode"] == "replace"
    assert result["photos_after"] == [new_url]
    assert result["photos_lost"] == existing
    assert captured["payload"]["Item"]["PictureDetails"]["PictureURL"] == [new_url]


def test_revise_pictures_truncates_above_24_with_warning() -> None:
    # 24 existing + 5 new in append mode → 29 → truncated to 24, dropping 5.
    existing = [f"https://eps/{i:02d}.jpg" for i in range(24)]
    new_paths = [f"/tmp/new{i}.jpg" for i in range(5)]
    new_urls = [f"https://eps/n{i}.jpg" for i in range(5)]
    captured = {}

    def _exec_side_effect(verb, payload, *args, **kwargs):
        if verb == "GetItem":
            return _fake_get_item(existing)
        if verb == "ReviseFixedPriceItem":
            captured["payload"] = payload
            return _fake_revise_response()

    with (
        patch("ebay.pictures.execute_with_retry", side_effect=_exec_side_effect),
        patch("ebay.pictures.preprocess_for_ebay", return_value=b"x"),
        patch("ebay.pictures.upload_one", side_effect=new_urls),
    ):
        result = _run(revise_pictures(item_id="123", photo_paths=new_paths, mode="append"))

    assert result["truncated"] is True
    assert result["truncated_count"] == 5
    assert result["photos_count_after"] == 24
    # L13 fix (Ralph deferred Opus): truncate-from-end preserves index 0
    # AND every just-uploaded URL. Pre-fix code dropped the newest entries.
    assert captured["payload"]["Item"]["PictureDetails"]["PictureURL"][0] == existing[0]
    final_urls = captured["payload"]["Item"]["PictureDetails"]["PictureURL"]
    for new_url in new_urls:
        assert new_url in final_urls, f"L13: newly-uploaded {new_url} was dropped"
    # Newest URLs sit at the tail (caller order preserved within composed[-23:]).
    assert final_urls[-len(new_urls) :] == new_urls


def test_revise_pictures_l13_dropped_oldest_reported_in_photos_lost() -> None:
    """L13 fix (Ralph deferred Opus) — append+overflow surfaces the oldest URLs
    that fell out via `photos_lost` so the operator can audit what was dropped.
    """
    existing = [f"https://eps/{i:02d}.jpg" for i in range(24)]
    new_paths = [f"/tmp/new{i}.jpg" for i in range(3)]
    new_urls = [f"https://eps/n{i}.jpg" for i in range(3)]

    def _exec_side_effect(verb, payload, *args, **kwargs):
        if verb == "GetItem":
            return _fake_get_item(existing)
        if verb == "ReviseFixedPriceItem":
            return _fake_revise_response()

    with (
        patch("ebay.pictures.execute_with_retry", side_effect=_exec_side_effect),
        patch("ebay.pictures.preprocess_for_ebay", return_value=b"x"),
        patch("ebay.pictures.upload_one", side_effect=new_urls),
    ):
        result = _run(revise_pictures(item_id="123", photo_paths=new_paths, mode="append"))

    # 24 existing + 3 new = 27 -> drop 3 oldest non-index-0 entries (existing[1..3]).
    assert result["truncated_count"] == 3
    assert result["photos_lost"] == existing[1:4]
    # `photos_after` retains gallery + (existing[4..23]) + new[0..2]
    assert result["photos_after"][0] == existing[0]
    assert result["photos_after"][-3:] == new_urls


def test_revise_pictures_l13_truncate_to_cap_helper_unit() -> None:
    """L13 fix (Ralph deferred Opus) -- direct unit test for `_truncate_to_cap`."""
    from ebay.listings import MAX_PICTURE_URLS
    from ebay.pictures import _truncate_to_cap

    # No overflow: short-circuit.
    composed = [f"u{i}" for i in range(5)]
    kept, dropped, n = _truncate_to_cap(composed, "append")
    assert kept == composed
    assert dropped == []
    assert n == 0

    # Append + overflow by 1: drop composed[1], keep gallery + last 23.
    composed = [f"u{i}" for i in range(MAX_PICTURE_URLS + 1)]
    kept, dropped, n = _truncate_to_cap(composed, "append")
    assert kept[0] == composed[0]
    assert kept[-1] == composed[-1]
    assert dropped == [composed[1]]
    assert n == 1
    assert len(kept) == MAX_PICTURE_URLS

    # Replace + overflow: head-slice (caller order is intent).
    composed = [f"u{i}" for i in range(MAX_PICTURE_URLS + 5)]
    kept, dropped, n = _truncate_to_cap(composed, "replace")
    assert kept == composed[:MAX_PICTURE_URLS]
    assert dropped == composed[MAX_PICTURE_URLS:]
    assert n == 5


def test_revise_pictures_echoes_shipping_details() -> None:
    """eBay overwrites ShippingDetails with default if not echoed — verify echo-back."""
    captured = {}

    def _exec_side_effect(verb, payload, *args, **kwargs):
        if verb == "GetItem":
            return _fake_get_item([])
        if verb == "ReviseFixedPriceItem":
            captured["payload"] = payload
            return _fake_revise_response()

    with (
        patch("ebay.pictures.execute_with_retry", side_effect=_exec_side_effect),
        patch("ebay.pictures.preprocess_for_ebay", return_value=b"x"),
        patch("ebay.pictures.upload_one", return_value="https://eps/x.jpg"),
    ):
        _run(revise_pictures(item_id="123", photo_paths=["/tmp/a.jpg"], mode="append"))

    # Built payload includes ShippingDetails extracted from the live Item.
    shipping = captured["payload"]["Item"]["ShippingDetails"]
    assert shipping["ShippingType"] == "Flat"
    sso = shipping["ShippingServiceOptions"]
    if isinstance(sso, list):
        sso = sso[0]
    assert sso["ShippingService"] == "UK_RoyalMailSecondClassStandard"


def test_revise_pictures_audit_log_entry(tmp_path, monkeypatch) -> None:
    """Audit-log entry per call (AC: existing audit_log_write)."""
    monkeypatch.setattr("ebay.listings._AUDIT_LOG_DIR", tmp_path)
    monkeypatch.setattr("ebay.listings._AUDIT_LOG_PATH", tmp_path / "audit.log")
    existing = ["https://eps/a.jpg"]

    def _exec_side_effect(verb, payload, *args, **kwargs):
        if verb == "GetItem":
            return _fake_get_item(existing)
        if verb == "ReviseFixedPriceItem":
            return _fake_revise_response()

    with (
        patch("ebay.pictures.execute_with_retry", side_effect=_exec_side_effect),
        patch("ebay.pictures.preprocess_for_ebay", return_value=b"x"),
        patch("ebay.pictures.upload_one", return_value="https://eps/n.jpg"),
    ):
        _run(revise_pictures(item_id="123", photo_paths=["/tmp/a.jpg"], mode="append"))

    log_path = tmp_path / "audit.log"
    assert log_path.exists()
    line = json.loads(log_path.read_text().strip())
    assert line["item_id"] == "123"
    assert line["fields_changed"] == ["picture_urls"]
    assert line["success"] is True


# ---------- MCP wrapper ----------


def test_mcp_wrapper_returns_json_string() -> None:
    """server.revise_pictures wraps core fn + serialises to JSON."""
    existing = ["https://eps/a.jpg"]

    def _exec_side_effect(verb, payload, *args, **kwargs):
        if verb == "GetItem":
            return _fake_get_item(existing)
        if verb == "ReviseFixedPriceItem":
            return _fake_revise_response()

    with (
        patch("ebay.pictures.execute_with_retry", side_effect=_exec_side_effect),
        patch("ebay.pictures.preprocess_for_ebay", return_value=b"x"),
        patch("ebay.pictures.upload_one", return_value="https://eps/n.jpg"),
    ):
        raw = _run(server.revise_pictures(item_id="123", photo_paths=["/tmp/a.jpg"]))

    body = json.loads(raw)
    assert body["ok"] is True
    assert body["item_id"] == "123"


def test_mcp_wrapper_serialises_validation_error() -> None:
    """ValueError from core → JSON error envelope, not a raise."""
    raw = _run(server.revise_pictures(item_id="123", photo_paths=[], mode="append"))
    body = json.loads(raw)
    assert "error" in body
    assert "at least 1 path" in body["error"]


def test_mcp_wrapper_replace_without_confirm() -> None:
    raw = _run(server.revise_pictures(item_id="123", photo_paths=["/tmp/a.jpg"], mode="replace"))
    body = json.loads(raw)
    assert "error" in body
    assert "confirm=True" in body["error"]
