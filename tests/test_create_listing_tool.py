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
        title or f'Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD {oem_model}'
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
    r.reply.Item.PictureDetails.PictureURL = [f"https://i.ebayimg.com/x{i}" for i in range(photos)]
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
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=True,
                    )
                )

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
                title='Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD ST2000NX0253',
                qty=1,
                condition_id=3000,
                photos=2,
                mpn="ST2000NX0253",
                brand="Seagate",
            )
        raise AssertionError(f"unexpected verb {verb}")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=False,
                    )
                )

    result = json.loads(raw)
    assert result["success"] is True
    assert result["item_id"] == "123456789"
    assert UUID_RE.match(result["uuid"])
    assert UUID_RE.match(captured_payload["Item"]["UUID"])
    assert captured_payload["Item"]["UUID"] == result["uuid"]


def test_create_listing_apply_enables_best_offer_with_qty_tier_thresholds(tmp_path: Path) -> None:
    """Operator policy: every new listing is born with Best Offer ON; thresholds from qty tier."""
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
                title='Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD ST2000NX0253',
                qty=1, condition_id=3000, photos=2, mpn="ST2000NX0253", brand="Seagate",
            )
        raise AssertionError(f"unexpected verb {verb}")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=False,
                    )
                )

    result = json.loads(raw)
    assert result["success"] is True
    item = captured_payload["Item"]
    # Best Offer ON by default (no explicit arg) — policy enforced by code
    assert item["BestOfferDetails"]["BestOfferEnabled"] == "true"
    # qty=1 → 95% accept on £49.99 → £47 (math.floor); decline 75% → £37
    assert item["ListingDetails"]["BestOfferAutoAcceptPrice"]["#text"] == "47.00"
    assert item["ListingDetails"]["MinimumBestOfferPrice"]["#text"] == "37.00"
    assert result["after"]["best_offer"]["enabled"] is True
    assert result["after"]["best_offer"]["auto_accept_gbp"] == 47


def test_create_listing_best_offer_disabled_when_opted_out(tmp_path: Path) -> None:
    """best_offer_enabled=False (explicit operator opt-out) emits no Best Offer block."""
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
                title='Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD ST2000NX0253',
                qty=1, condition_id=3000, photos=2, mpn="ST2000NX0253", brand="Seagate",
            )
        raise AssertionError(f"unexpected verb {verb}")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=False,
                        best_offer_enabled=False,
                    )
                )

    result = json.loads(raw)
    assert result["success"] is True
    assert "BestOfferDetails" not in captured_payload["Item"]
    assert result["after"]["best_offer"]["enabled"] is False


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
                title='Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD ST2000NX0253',
                qty=1,
                condition_id=3000,
                photos=2,
                mpn="ST2000NX0253",
                brand="Seagate",
            )
        raise AssertionError(f"unexpected verb {verb}")

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=False,
                    )
                )

    result = json.loads(raw)
    assert result["success"] is True
    assert result["item_id"] == "777777777"
    assert result["duplicate_invocation"] is True


def test_create_listing_missing_photos_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path, num_photos=0)
    server._create_listing_uuid_cache.clear()

    raw = _run(
        server.create_listing(
            folder_path=str(folder),
            price=49.99,
            quantity=1,
            condition="Used",
            has_caddy=False,
            dry_run=True,
            picture_urls=None,
        )
    )
    result = json.loads(raw)
    assert "error" in result
    assert "no IMG*.jpg or visual-*/SMART-*/DISK-TEST-VISUAL-*.png photos" in result["error"]


def test_create_listing_unknown_mpn_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path, oem_model="ST9999NX9999")

    raw = _run(
        server.create_listing(
            folder_path=str(folder),
            price=49.99,
            quantity=1,
            condition="Used",
            has_caddy=False,
            dry_run=True,
        )
    )
    result = json.loads(raw)
    assert "error" in result
    assert "Unknown MPN" in result["error"]
    assert "ST9999NX9999" in result["error"]
    assert "hdd_specs.py" in result["error"]


def test_create_listing_invalid_condition_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path)

    raw = _run(
        server.create_listing(
            folder_path=str(folder),
            price=49.99,
            quantity=1,
            condition="Refurbished",  # not in CONDITION_MAP
            has_caddy=False,
            dry_run=True,
        )
    )
    result = json.loads(raw)
    assert "error" in result
    assert "invalid condition" in result["error"].lower()


def test_create_listing_title_over_80_chars_fails_loudly(tmp_path: Path) -> None:
    long_title = "X" * 90
    folder = _mk_product_folder(tmp_path, title=long_title)

    raw = _run(
        server.create_listing(
            folder_path=str(folder),
            price=49.99,
            quantity=1,
            condition="Used",
            has_caddy=False,
            dry_run=True,
        )
    )
    result = json.loads(raw)
    assert "error" in result
    assert "80-char" in result["error"]


def test_create_listing_price_zero_fails_loudly(tmp_path: Path) -> None:
    folder = _mk_product_folder(tmp_path)

    raw = _run(
        server.create_listing(
            folder_path=str(folder),
            price=0.0,
            quantity=1,
            condition="Used",
            has_caddy=False,
            dry_run=True,
        )
    )
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
        raise AssertionError(
            f"unexpected verb {verb} — Add/Verify should not fire after upload failure"
        )

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=False,
                    )
                )

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
                title='Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD ST2000NX0253',
                qty=1,
                condition_id=3000,
                photos=2,
                mpn="ST2000NX0253",
                brand="Seagate",
            )
        raise AssertionError(verb)

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=False,
                    )
                )

    result = json.loads(raw)
    # update_listing schema keys: success, item_id, fields_updated, before, after
    # create_listing: success, item_id, listing_url, uuid, fees, verify_warnings, before, after
    required = {"success", "item_id", "before", "after"}
    assert required.issubset(result.keys())
    assert result["before"] is None  # no before on CREATE
    assert "title" in result["after"]
    assert "picture_count" in result["after"]


def test_create_listing_rejects_non_directory(tmp_path: Path) -> None:
    raw = _run(
        server.create_listing(
            folder_path=str(tmp_path / "does-not-exist"),
            price=49.99,
            quantity=1,
            condition="Used",
            has_caddy=False,
            dry_run=True,
        )
    )
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
                raw1 = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=True,
                    )
                )
                raw2 = _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=True,
                    )
                )
    r1, r2 = json.loads(raw1), json.loads(raw2)
    assert r1["uuid"] == r2["uuid"]


def test_create_listing_distinct_titles_get_distinct_uuids(tmp_path: Path) -> None:
    """Two variants from ONE folder (distinct titles via description_html) get
    distinct UUIDs — else the 2nd AddFixedPriceItem dedupes against the 1st."""
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

    html_a = '<div class="copy-block">Variant A Title One ST2000NX0253</div><h1>x</h1>'
    html_b = '<div class="copy-block">Variant B Title Two Low Hours ST2000NX0253</div><h1>x</h1>'
    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw_a = _run(
                    server.create_listing(
                        folder_path=str(folder), price=49.99, quantity=1,
                        condition="Used", has_caddy=False, dry_run=True,
                        description_html=html_a,
                    )
                )
                raw_b = _run(
                    server.create_listing(
                        folder_path=str(folder), price=39.99, quantity=1,
                        condition="Used", has_caddy=False, dry_run=True,
                        description_html=html_b,
                    )
                )
    ra, rb = json.loads(raw_a), json.loads(raw_b)
    assert ra["uuid"] != rb["uuid"], "distinct-title variants must get distinct UUIDs"


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
                _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=True,
                    )
                )

    nvl = captured["payload"]["Item"]["ItemSpecifics"]["NameValueList"]
    tr_row = next(r for r in nvl if r["Name"] == "Transfer Rate")
    assert tr_row["Value"] == ["12G"]


def test_create_listing_transfer_rate_3g_from_title(tmp_path: Path) -> None:
    """SATA II / 3Gb/s in title → Transfer Rate = 3G (P3.5 branch coverage)."""
    folder = _mk_product_folder(
        tmp_path,
        oem_model="ST2000NX0253",
        title='Seagate Enterprise 2TB 7200RPM 15mm 2.5" SATA II HDD ST2000NX0253',
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
                _run(
                    server.create_listing(
                        folder_path=str(folder),
                        price=49.99,
                        quantity=1,
                        condition="Used",
                        has_caddy=False,
                        dry_run=True,
                    )
                )

    nvl = captured["payload"]["Item"]["ItemSpecifics"]["NameValueList"]
    tr_row = next(r for r in nvl if r["Name"] == "Transfer Rate")
    assert tr_row["Value"] == ["3G"]


def test_create_listing_glob_case_insensitive_JPG(tmp_path: Path) -> None:
    """_glob_label_photos discovers both .jpg and .JPG (iPhone naming)."""
    folder = tmp_path / "ST2000NX0253"
    folder.mkdir()
    for name in ("IMG20260420090000.jpg", "IMG20260420090001.JPG"):
        im = Image.new("RGB", (100, 100), (200, 100, 50))
        buf = BytesIO()
        im.save(buf, format="JPEG")
        (folder / name).write_bytes(buf.getvalue())
    found = server._glob_label_photos(folder)
    assert len(found) == 2, f"expected lowercase AND uppercase JPG, got {found}"
    assert any(p.endswith(".jpg") for p in found)
    assert any(p.endswith(".JPG") for p in found)


def test_build_21_field_specifics_raises_on_missing_required() -> None:
    """Fail-Fast: HDD_SPECS with a None required field fails loud (no silent '')."""
    broken = {
        "brand": None,
        "family": "Enterprise Capacity",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    }
    with pytest.raises(ValueError, match=r"empty/None required field"):
        server._build_21_field_specifics(
            "ST2000NX0253",
            'Seagate 2TB 7200RPM 2.5" SATA III HDD',
            has_caddy=False,
            specs=broken,
        )


# ---- worksheet-scaffolding stripping (publish body only) ----

# A realistic operator authoring worksheet: document chrome + <h1> + a "Title"
# copy-block + an Item-Specifics reference table + section headings, THEN the
# real listing body (warning + section divs). Only the body may reach buyers.
_WORKSHEET = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>x</title>
<style>body{font-family:Arial}</style></head>
<body>
<h1>Seagate Enterprise Capacity 2TB <span class="condition-badge">USED</span></h1>
<p class="note">STANDARD POOL listing. low-hours file is listing-used-low-poh.html.</p>
<h2>Title <span class="note">(paste into eBay title field)</span></h2>
<div class="copy-block">Seagate Enterprise Capacity 2TB 7200RPM 2.5" SATA III HDD ST2000NX0253</div>
<h2>Item Specifics <span class="note">(eBay item specifics fields)</span></h2>
<table><tr><th>Field</th><th>Value</th></tr><tr><td>Brand</td><td>Seagate</td></tr></table>
<h2>Description <span class="note">(paste into eBay description editor)</span></h2>
<div class="warning" style="background:#ffebee;">
<p class="warning-title">IMPORTANT</p><p>Body warning text.</p></div>
<div class="section"><h3>Overview</h3><p>Real description body.</p></div>
</body></html>"""

_SCAFFOLDING_MARKERS = [
    "paste into eBay",
    "copy-block",
    "STANDARD POOL",
    "listing-used-low-poh.html",
    "Item Specifics",
    "condition-badge",
    "<h1",
    "<!DOCTYPE",
    "<style",
    "<head",
]


def test_extract_description_body_strips_worksheet_scaffolding() -> None:
    out = server._extract_description_body(_WORKSHEET)
    assert out.startswith('<div class="warning"')
    assert "warning-title" in out
    assert "Real description body." in out
    for marker in _SCAFFOLDING_MARKERS:
        assert marker not in out, f"worksheet scaffolding leaked into body: {marker!r}"


def test_extract_description_body_clean_body_unchanged() -> None:
    clean = '<div class="warning"><p>w</p></div>\n<div class="section"><p>b</p></div>'
    assert server._extract_description_body(clean) == clean


def test_extract_description_body_fallback_strips_chrome_and_scaffolding() -> None:
    """No warning/section anchor (template/minimal body): still strip chrome + copy-block + <h1>."""
    html = (
        '<html><body><div class="copy-block">\nTitle: X\n</div>'
        "<h1>X</h1><p>Body.</p></body></html>"
    )
    assert server._extract_description_body(html) == "<p>Body.</p>"


def test_create_listing_publishes_body_only_not_worksheet(tmp_path: Path) -> None:
    """End-to-end: the AddFixedPriceItem payload must carry the listing body
    and NONE of the worksheet scaffolding (regression for the verbatim-publish bug)."""
    folder = tmp_path / "ST2000NX0253"
    folder.mkdir()
    im = Image.new("RGB", (400, 300), (10, 100, 200))
    buf = BytesIO()
    im.save(buf, format="JPEG")
    (folder / "IMG20260420000000.jpg").write_bytes(buf.getvalue())
    (folder / "listing-used.html").write_text(_WORKSHEET, encoding="utf-8")
    server._create_listing_uuid_cache.clear()
    captured: dict = {}

    def fake_exec(verb: str, *args, **kwargs):
        data = args[0] if args else {}
        if verb == "UploadSiteHostedPictures":
            r = MagicMock()
            r.reply.SiteHostedPictureDetails.FullURL = "https://i.ebayimg.com/p/$_57.JPG"
            return r
        if verb == "AddFixedPriceItem":
            captured.update(data)
            return _fake_add_response("123")
        if verb == "GetItem":
            return _fake_getitem_response(
                title="Seagate Enterprise Capacity 2TB 7200RPM 2.5\" SATA III HDD ST2000NX0253",
                qty=1, condition_id=3000, photos=1, mpn="ST2000NX0253", brand="Seagate",
            )
        raise AssertionError(verb)

    with patch("server.execute_with_retry", side_effect=fake_exec):
        with patch("ebay.photos.execute_with_retry", side_effect=fake_exec):
            with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
                raw = _run(
                    server.create_listing(
                        folder_path=str(folder), price=49.99, quantity=1,
                        condition="Used", has_caddy=False, dry_run=False,
                    )
                )

    result = json.loads(raw)
    assert result["success"] is True
    # Title still derives from the worksheet copy-block.
    assert "ST2000NX0253" in result["after"]["title"]
    # Published description = body only, no scaffolding.
    desc = captured["Item"]["Description"]
    assert "warning-title" in desc
    assert "Real description body." in desc
    for marker in _SCAFFOLDING_MARKERS:
        assert marker not in desc, f"worksheet scaffolding reached the live payload: {marker!r}"


def test_update_listing_accepts_2750_used_excellent(tmp_path: Path) -> None:
    """update_listing validation must accept 2750 (Used - Excellent) per CONDITION_MAP."""
    # This is a pure validation test — no eBay API call needed because the
    # validation happens before any call. We mock GetItem just in case.
    with patch("server.execute_with_retry") as mock_exec:
        mock_exec.return_value = MagicMock()
        mock_exec.return_value.reply.Item = None
        raw = _run(
            server.update_listing(
                item_id="12345",
                condition_id=2750,
                dry_run=True,
            )
        )
    result = json.loads(raw)
    # We expect either a "not found" or a diff — NOT an "invalid condition_id" error
    assert "invalid condition_id" not in str(result)
