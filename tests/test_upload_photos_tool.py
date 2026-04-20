"""Unit tests for the upload_photos MCP tool (P2.8)."""

import asyncio
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

import server


def _mk_photo(tmp_path: Path, idx: int) -> Path:
    p = tmp_path / f"photo_{idx}.png"
    im = Image.new("RGB", (200, 200), (idx * 10 % 255, 128, 255))
    buf = BytesIO()
    im.save(buf, format="PNG")
    p.write_bytes(buf.getvalue())
    return p


def _run(coro: object) -> str:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_upload_photos_dry_run_returns_preview_no_api_calls(tmp_path: Path) -> None:
    paths = [str(_mk_photo(tmp_path, i)) for i in range(3)]

    with patch("server.upload_one") as mock_upload:
        raw = _run(server.upload_photos(paths, dry_run=True))

    result = json.loads(raw)
    assert result["dry_run"] is True
    assert result["would_upload"] == 3
    assert len(result["preview"]) == 3
    for entry in result["preview"]:
        assert entry["rejected"] is False
        assert entry["size_bytes_after_preprocess"] > 0
    assert mock_upload.call_count == 0


def test_upload_photos_rejects_empty_list() -> None:
    raw = _run(server.upload_photos([], dry_run=False))
    result = json.loads(raw)
    assert "error" in result
    assert "at least 1" in result["error"]


def test_upload_photos_rejects_25th_photo(tmp_path: Path) -> None:
    paths = [str(_mk_photo(tmp_path, i)) for i in range(25)]
    raw = _run(server.upload_photos(paths, dry_run=False))
    result = json.loads(raw)
    assert "error" in result
    assert "exceeds" in result["error"]
    assert "24" in result["error"]


def test_upload_photos_partial_failure_returns_urls_so_far(tmp_path: Path) -> None:
    """Fail on 3rd of 5 uploads — first 2 URLs preserved in response."""
    paths = [str(_mk_photo(tmp_path, i)) for i in range(5)]
    call_log: list[int] = []

    def fake_upload(bytes_data: bytes) -> str:
        idx = len(call_log)
        call_log.append(idx)
        if idx == 2:  # third upload (0-indexed)
            raise RuntimeError("eBay API boom")
        return f"https://i.ebayimg.com/images/g/x{idx}/$_57.JPG"

    with patch("server.upload_one", side_effect=fake_upload):
        with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):  # fast test
            raw = _run(server.upload_photos(paths, dry_run=False))

    result = json.loads(raw)
    assert result["success"] is False
    assert result["failed_at_index"] == 2
    assert result["failed_path"] == paths[2]
    assert len(result["urls_uploaded_so_far"]) == 2
    assert "eBay API boom" in result["error"]


def test_upload_photos_success_returns_urls_and_total_chars(tmp_path: Path) -> None:
    paths = [str(_mk_photo(tmp_path, i)) for i in range(2)]
    fake_urls = [
        "https://i.ebayimg.com/images/g/aaa/$_57.JPG",
        "https://i.ebayimg.com/images/g/bbb/$_57.JPG",
    ]

    with patch("server.upload_one", side_effect=fake_urls):
        with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
            raw = _run(server.upload_photos(paths, dry_run=False))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["urls"] == fake_urls
    assert result["total_url_chars"] == sum(len(u) for u in fake_urls)
    assert result["warnings"] == []


def test_upload_photos_warns_on_joined_chars_over_cap(tmp_path: Path) -> None:
    """Hit the 3975-char soft cap — warnings list is populated but success=True."""
    paths = [str(_mk_photo(tmp_path, i)) for i in range(20)]
    # Each URL ~200 chars × 20 = 4000 chars > 3975
    big_urls = [
        "https://i.ebayimg.com/" + ("x" * 180) + f"/$_57.JPG?id={i}"
        for i in range(20)
    ]

    with patch("server.upload_one", side_effect=big_urls):
        with patch("server.UPLOAD_RATE_LIMIT_SLEEP_SECONDS", 0):
            raw = _run(server.upload_photos(paths, dry_run=False))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["total_url_chars"] >= 3975
    assert len(result["warnings"]) == 1
    assert "3975" in result["warnings"][0]


def test_upload_photos_dry_run_flags_xcf(tmp_path: Path) -> None:
    good = _mk_photo(tmp_path, 0)
    bad = tmp_path / "bad.xcf"
    bad.write_bytes(b"xcf data")

    with patch("server.upload_one") as mock_upload:
        raw = _run(server.upload_photos([str(good), str(bad)], dry_run=True))

    result = json.loads(raw)
    assert result["dry_run"] is True
    assert result["would_upload"] == 1
    assert result["preview"][0]["rejected"] is False
    assert result["preview"][1]["rejected"] is True
    assert "XCF not supported" in result["preview"][1]["reason"]
    assert mock_upload.call_count == 0
