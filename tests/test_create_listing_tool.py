"""Unit tests for the create_listing MCP tool (P3.15)."""

import asyncio
import json
import re
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

import server

UUID_RE = re.compile(r"^[0-9A-F]{32}$")


def _mk_product_folder(
    tmp_path: Path,
    oem_model: str = "ST2000NX0253",
    num_photos: int = 2,
    html_suffix: str = "used",
    title: str | None = None,
) -> Path:
    folder = tmp_path / oem_model
    folder.mkdir(parents=True, exist_ok=True)

    # Synthesise IMG*.jpg label photos that match LABEL_PHOTO_REGEX
    for i in range(num_photos):
        im = Image.new("RGB", (400, 300), (i * 20 % 255, 100, 200))
        buf = BytesIO()
        im.save(buf, format="JPEG")
        photo = folder / f"IMG20260420{i:06d}.jpg"
        photo.write_bytes(buf.getvalue())

    # Synthesise listing-<suffix>.html with a copy-block title row
    resolved_title = (
        title
        or f'Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD {oem_model}'
    )
    html = f"""<html><body>
<div class="copy-block">
Title: {resolved_title}
</div>
<h1>{resolved_title}</h1>
<p>Drive description body.</p>
</body></html>"""
    (folder / f"listing-{html_suffix}.html").write_text(html, encoding="utf-8")
    return folder


def _fake_verify_response() -> MagicMock:
    r = MagicMock()
    r.reply.Errors = None
    r.reply.Fees.Fee = [
        MagicMock(Name="InsertionFee", Fee=MagicMock(value="0.00", _currencyID="GBP"))
    ]
    return r


def _fake_add_response(item_id: str, duplicate: bool = False) -> MagicMock:
    r = MagicMock()
    r.reply.ItemID = item_id
    r.reply.Fees.Fee = [
        MagicMock(Name="InsertionFee", Fee=MagicMock(value="0.35", _currencyID="GBP"))
    ]
    if duplicate:
        r.reply.DuplicateInvocationDetails = MagicMock(ItemID=item_id)
    else:
        r.reply.DuplicateInvocationDetails = None
    return r


def _fake_getitem_response(
    title: str, qty: int, condition_id: int, photos: int, mpn: str, brand: str
) -> MagicMock:
    r = MagicMock()
    r.reply.Item = MagicMock()
    # listing_to_dict reads .ItemID, .Title, .ConditionID, .ConditionDisplayName, .Quantity,
    # .SellingStatus.CurrentPrice.value / ._currencyID, .ListingDetails.ViewItemURL,
    # .ItemSpecifics.NameValueList, .PictureDetails.PictureURL, .Description, .PrimaryCategory.
    r.reply.Item.ItemID = "9999"
    r.reply.Item.Title = title
    r.reply.Item.ConditionID = str(condition_id)
    r.reply.Item.ConditionDisplayName = "Used"
    r.reply.Item.Quantity = qty
    r.reply.Item.QuantityAvailable = qty
    r.reply.Item.SellingStatus.CurrentPrice.value = "49.99"
    r.reply.Item.SellingStatus.CurrentPrice._currencyID = "GBP"
    r.reply.Item.ListingDetails.ViewItemURL = "https://www.ebay.co.uk/itm/9999"
    r.reply.Item.PrimaryCategory.CategoryID = "56083"
    r.reply.Item.PrimaryCategory.CategoryName = "Internal Hard Disk Drives"
    r.reply.Item.SubTitle = None
    nvl = [
        MagicMock(Name="Brand", Value=brand),
        MagicMock(Name="MPN", Value=mpn),
    ]
    r.reply.Item.ItemSpecifics.NameValueList = nvl
    r.reply.Item.PictureDetails.PictureURL = [
        f"https://i.ebayimg.com/x{i}" for i in range(photos)
    ]
    r.reply.Item.Description = "<html>...</html>"
    r.reply.Item.WatchCount = 0
    r.reply.Item.HitCount = 0
    return r


def _run(coro: object) -> str:
    return asyncio.run(coro)  # type: ignore[arg-type]


# ---- P3.15 tests ----


def test_create_listing_dry_run_calls_verify_not_add(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path)
    calls: list[str] = []

    def fake_exec(verb: str, *args, **kwargs):
        calls.append(verb)
        if verb == "UploadSiteHostedPictures":
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = (
                f"https://i.ebayimg.com/p{len(calls)}/$_57.JPG"
            )
            return r
        if verb == "VerifyAddFixedPriceItem":
            return _fake_verify_response()
        raise AssertionError(f"unexpected verb {verb}")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=True,
                ))

    result = json.loads(raw)
    assert result["dry_run"] is True
    assert UUID_RE.match(result["uuid"])
    assert result["errors"] == []
    assert len(result["fees"]) == 1
    assert "VerifyAddFixedPriceItem" in calls
    assert "AddFixedPriceItem" not in calls


def test_create_listing_apply_sets_uuid_in_payload(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path)
    server._create_listing_uuid_cache.clear()
    captured_payload: dict = {}

    def fake_exec(verb: str, *args, **kwargs):
        data = args[0] if args else kwargs.get("data", {})
        if verb == "UploadSiteHostedPictures":
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = "https://i.ebayimg.com/p/$_57.JPG"
            return r
        if verb == "AddFixedPriceItem":
            captured_payload.update(data)
            return _fake_add_response("123456789")
        if verb == "GetItem":
            return _fake_getitem_response(
                title="Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5\" SATA III HDD ST2000NX0253",
                qty=1, condition_id=3000, photos=2,
                mpn="ST2000NX0253", brand="Seagate",
            )
        raise AssertionError(f"unexpected verb {verb}")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=False,
                ))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["item_id"] == "123456789"
    assert UUID_RE.match(result["uuid"])
    assert UUID_RE.match(captured_payload["Item"]["UUID"])
    assert captured_payload["Item"]["UUID"] == result["uuid"]


def test_create_listing_uuid_replay_returns_existing_itemid(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path)
    server._create_listing_uuid_cache.clear()

    def fake_exec(verb: str, *args, **kwargs):
        if verb == "UploadSiteHostedPictures":
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = "https://i.ebayimg.com/p/$_57.JPG"
            return r
        if verb == "AddFixedPriceItem":
            return _fake_add_response("777777777", duplicate=True)
        if verb == "GetItem":
            return _fake_getitem_response(
                title="Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5\" SATA III HDD ST2000NX0253",
                qty=1, condition_id=3000, photos=2,
                mpn="ST2000NX0253", brand="Seagate",
            )
        raise AssertionError(f"unexpected verb {verb}")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=False,
                ))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["item_id"] == "777777777"
    assert result["duplicate_invocation"] is True


def test_create_listing_missing_photos_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path, num_photos=0)
    server._create_listing_uuid_cache.clear()

    raw = _run(server.create_listing(
        folder_path=str(folder), price=49.99, quantity=1,
        condition="Used", has_caddy=False, dry_run=True, picture_urls=None,
    ))
    result = json.loads(raw)
    assert "error" in result
    assert "no IMG*.jpg photos" in result["error"]


def test_create_listing_unknown_mpn_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path, oem_model="ST9999NX9999")

    raw = _run(server.create_listing(
        folder_path=str(folder), price=49.99, quantity=1,
        condition="Used", has_caddy=False, dry_run=True,
    ))
    result = json.loads(raw)
    assert "error" in result
    assert "Unknown MPN" in result["error"]
    assert "ST9999NX9999" in result["error"]
    assert "hdd_specs.py" in result["error"]


def test_create_listing_invalid_condition_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path)

    raw = _run(server.create_listing(
        folder_path=str(folder), price=49.99, quantity=1,
        condition="Refurbished",  # not in CONDITION_MAP
        has_caddy=False, dry_run=True,
    ))
    result = json.loads(raw)
    assert "error" in result
    assert "invalid condition" in result["error"].lower()


def test_create_listing_title_over_80_chars_fails_loudly(tmp_path: Path) -> None:
    long_title = "X" * 90
    folder = _mk_product_folder(tmp_path, title=long_title)

    raw = _run(server.create_listing(
        folder_path=str(folder), price=49.99, quantity=1,
        condition="Used", has_caddy=False, dry_run=True,
    ))
    result = json.loads(raw)
    assert "error" in result
    assert "80-char" in result["error"]


def test_create_listing_price_zero_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path)

    raw = _run(server.create_listing(
        folder_path=str(folder), price=0.0, quantity=1,
        condition="Used", has_caddy=False, dry_run=True,
    ))
    result = json.loads(raw)
    assert "error" in result
    assert "price must be > 0" in result["error"]


def test_create_listing_partial_upload_failure_preserves_uploaded_urls(
    tmp_path: Path,
) -> None:
    folder = _mk_product_folder(tmp_path, num_photos=5)
    server._create_listing_uuid_cache.clear()

    call_log: list[int] = []

    def fake_exec(verb: str, *args, **kwargs):
        if verb == "UploadSiteHostedPictures":
            idx = len(call_log)
            call_log.append(idx)
            if idx == 2:
                raise RuntimeError("eBay upload boom")
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = f"https://i.ebayimg.com/p{idx}/$_57.JPG"
            return r
        raise AssertionError(f"unexpected verb {verb} — Add/Verify should not fire after upload failure")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=False,
                ))

    result = json.loads(raw)
    assert "error" in result
    assert "photo upload failed" in result["error"]
    assert len(result["uploaded_urls"]) == 2
    assert result["uploaded_urls"][0].startswith("https://i.ebayimg.com/p0")


def test_create_listing_return_shape_matches_update_listing_schema(
    tmp_path: Path,
) -> None:
    """Top-level keys should be subset/superset of update_listing's schema keys."""
    folder = _mk_product_folder(tmp_path)
    server._create_listing_uuid_cache.clear()

    def fake_exec(verb: str, *args, **kwargs):
        if verb == "UploadSiteHostedPictures":
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = "https://i.ebayimg.com/x/$_57.JPG"
            return r
        if verb == "AddFixedPriceItem":
            return _fake_add_response("123")
        if verb == "GetItem":
            return _fake_getitem_response(
                title="Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5\" SATA III HDD ST2000NX0253",
                qty=1, condition_id=3000, photos=2,
                mpn="ST2000NX0253", brand="Seagate",
            )
        raise AssertionError(verb)

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=False,
                ))

    result = json.loads(raw)
    # update_listing schema keys: success, item_id, fields_updated, before, after
    # create_listing: success, item_id, listing_url, uuid, fees, verify_warnings, before, after
    required = {"success", "item_id", "before", "after"}
    assert required.issubset(result.keys())
    assert result["before"] is None  # no before on CREATE
    assert "title" in result["after"]
    assert "picture_count" in result["after"]


def test_create_listing_rejects_non_directory(tmp_path: Path) -> None:
    raw = _run(server.create_listing(
        folder_path=str(tmp_path / "does-not-exist"),
        price=49.99, quantity=1, condition="Used", has_caddy=False, dry_run=True,
    ))
    result = json.loads(raw)
    assert "error" in result
    assert "not a directory" in result["error"]


def test_create_listing_uuid_cache_stable_across_calls(tmp_path: Path) -> None:
    """Second call with same folder_path re-uses the cached UUID."""
    folder = _mk_product_folder(tmp_path)
    server._create_listing_uuid_cache.clear()

    def fake_exec(verb: str, *args, **kwargs):
        if verb == "UploadSiteHostedPictures":
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = "https://i.ebayimg.com/x/$_57.JPG"
            return r
        if verb == "VerifyAddFixedPriceItem":
            return _fake_verify_response()
        raise AssertionError(verb)

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw1 = _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=True,
                ))
                raw2 = _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=True,
                ))
    r1, r2 = json.loads(raw1), json.loads(raw2)
    assert r1["uuid"] == r2["uuid"]


def test_create_listing_transfer_rate_12g_from_title(tmp_path: Path) -> None:
    """Title authoritative for Transfer Rate per P3.5."""
    folder = _mk_product_folder(
        tmp_path,
        oem_model="ST2000NX0273",  # SAS 12G drive in HDD_SPECS
        title='Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SAS 12Gb/s HDD ST2000NX0273',
    )
    server._create_listing_uuid_cache.clear()
    captured: dict = {}

    def fake_exec(verb: str, *args, **kwargs):
        data = args[0] if args else {}
        if verb == "UploadSiteHostedPictures":
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = "https://i.ebayimg.com/p/$_57.JPG"
            return r
        if verb == "VerifyAddFixedPriceItem":
            captured["payload"] = data
            return _fake_verify_response()
        raise AssertionError(verb)

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                _run(server.create_listing(
                    folder_path=str(folder), price=49.99, quantity=1,
                    condition="Used", has_caddy=False, dry_run=True,
                ))

    nvl = captured["payload"]["Item"]["ItemSpecifics"]["NameValueList"]
    tr_row = next(r for r in nvl if r["Name"] == "Transfer Rate")
    assert tr_row["Value"] == ["12G"]
