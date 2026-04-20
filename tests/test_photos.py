"""Unit tests for ebay.photos (P2.5)."""

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from ebay import photos


def _png_bytes(w: int, h: int, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Synthesise a PNG in memory for tests."""
    im = Image.new("RGB", (w, h), color)
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _write_png(tmp_path: Path, w: int, h: int, name: str = "photo.png") -> Path:
    p = tmp_path / name
    p.write_bytes(_png_bytes(w, h))
    return p


def test_preprocess_strips_exif(tmp_path: Path) -> None:
    """EXIF is absent from the output JPEG (privacy — GPS / camera stripped)."""
    src = tmp_path / "in.jpg"
    im = Image.new("RGB", (2000, 1500), (0, 128, 255))
    exif = Image.Exif()
    exif[0x0112] = 3  # Orientation=180
    im.save(src, format="JPEG", exif=exif.tobytes())

    out = photos.preprocess_for_ebay(str(src))
    out_im = Image.open(BytesIO(out))
    # Pillow's getexif() on a JPEG saved without exif= returns an empty Exif.
    assert not dict(out_im.getexif())


def test_preprocess_downscales_oversized_dimensions(tmp_path: Path) -> None:
    """4000×3000 input → ≤ 1600 on longest side."""
    src = _write_png(tmp_path, 4000, 3000, "big.png")
    out = photos.preprocess_for_ebay(str(src))
    out_im = Image.open(BytesIO(out))
    assert max(out_im.size) <= 1600


def test_preprocess_leaves_small_dimensions_untouched(tmp_path: Path) -> None:
    """800×600 input → dimensions preserved (thumbnail is no-op when already small)."""
    src = _write_png(tmp_path, 800, 600, "small.png")
    out = photos.preprocess_for_ebay(str(src))
    out_im = Image.open(BytesIO(out))
    assert out_im.size == (800, 600)


def test_preprocess_rejects_xcf(tmp_path: Path) -> None:
    src = tmp_path / "foo.xcf"
    src.write_bytes(b"fake xcf bytes")  # content irrelevant — extension triggers reject
    with pytest.raises(ValueError, match=r"XCF not supported"):
        photos.preprocess_for_ebay(str(src))


def test_preprocess_xcf_message_verbatim(tmp_path: Path) -> None:
    """Exact-string match per issue P2.4."""
    src = tmp_path / "foo.xcf"
    src.write_bytes(b"x")
    try:
        photos.preprocess_for_ebay(str(src))
    except ValueError as e:
        assert str(e) == (
            "XCF not supported by eBay Picture Services. Export to JPG in GIMP "
            "(File → Export As) and retry."
        )


def test_preprocess_rejects_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.jpg"
    with pytest.raises(ValueError, match=r"does not exist"):
        photos.preprocess_for_ebay(str(missing))


def test_preprocess_rejects_oversized_file(tmp_path: Path) -> None:
    """> 12 MB file raises before Pillow sees it."""
    src = tmp_path / "huge.jpg"
    src.write_bytes(b"\x00" * (13 * 1024 * 1024))
    with pytest.raises(ValueError, match=r"exceeds 12 MB"):
        photos.preprocess_for_ebay(str(src))


def test_upload_one_returns_fullurl(monkeypatch: pytest.MonkeyPatch) -> None:
    """FullURL extracted from SiteHostedPictureDetails."""
    fake_response = MagicMock()
    fake_response.reply.SiteHostedPictureDetails.FullURL = (
        "https://i.ebayimg.com/images/g/abc/$_57.JPG"
    )
    mock_exec = MagicMock(return_value=fake_response)
    monkeypatch.setattr(photos, "execute_with_retry", mock_exec)

    url = photos.upload_one(b"fake-jpeg-bytes")

    assert url == "https://i.ebayimg.com/images/g/abc/$_57.JPG"
    assert mock_exec.call_count == 1
    call = mock_exec.call_args
    assert call.args[0] == "UploadSiteHostedPictures"
    assert call.args[1] == {"WarningLevel": "High", "PictureSet": "Supersize"}
    # Explicit kwarg check — files= forwarded (AP #18 no-kwargs-swallowing)
    assert "files" in call.kwargs
    files = call.kwargs["files"]
    assert files["file"][0] == "photo.jpg"
    assert files["file"][1] == b"fake-jpeg-bytes"
    assert files["file"][2] == "image/jpeg"
