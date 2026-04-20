"""
Photo preprocessing + eBay Picture Services upload.

preprocess_for_ebay: one-pass validation + EXIF-transpose + RGB convert +
  ≤ 1600×1600 thumbnail + JPEG q90 optimize progressive — strips EXIF.
upload_one: wraps UploadSiteHostedPictures multipart for a single image,
  returns the eBay-hosted FullURL.

HEIC input is supported best-effort via pillow-heif (optional — iPhone users
only). Missing → ImportError swallowed at module import, HEIC paths then
fail inside Pillow's Image.open with its own clear error.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from ebay.client import execute_with_retry, log_debug

try:  # iPhone HEIC support is opt-in.
    from pillow_heif import register_heif_opener  # type: ignore[import-not-found]

    register_heif_opener()
except ImportError:  # pragma: no cover — environment-dependent
    pass

MAX_FILE_BYTES = 12 * 1024 * 1024  # 12 MB hard cap (eBay rejects much larger)
MAX_DIMENSION = 1600  # eBay Zoom needs ≥ 1600 on long side; anything larger is waste.
JPEG_QUALITY = 90
XCF_REJECT_MSG = (
    "XCF not supported by eBay Picture Services. Export to JPG in GIMP "
    "(File → Export As) and retry."
)


def preprocess_for_ebay(path: str) -> bytes:
    """Validate + preprocess + encode a local image as eBay-ready JPEG bytes.

    Validation → load → EXIF-transpose → RGB → thumbnail(1600×1600) → JPEG q90.
    Strips EXIF (privacy — GPS / camera info removed) via save without exif= arg.

    Raises ValueError with clear message on:
      - missing path
      - XCF (GIMP native format — eBay rejects)
      - > MAX_FILE_BYTES (12 MB input)
    Lets PIL raise its own exception for unreadable / corrupt images.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"photo path does not exist: {path}")
    if p.suffix.lower() == ".xcf":
        raise ValueError(XCF_REJECT_MSG)
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        mb = size / (1024 * 1024)
        raise ValueError(
            f"photo {path} is {mb:.1f} MB — exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB limit"
        )

    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        im.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        return buf.getvalue()


def upload_one(bytes_data: bytes) -> str:
    """Upload a single preprocessed image to eBay Picture Services.

    Returns the FullURL from the SiteHostedPictureDetails block.

    Raises whatever execute_with_retry raises on API failure (caller handles).
    """
    log_debug(f"UploadSiteHostedPictures CALLING bytes={len(bytes_data)}")
    response = execute_with_retry(
        "UploadSiteHostedPictures",
        {"WarningLevel": "High", "PictureSet": "Supersize"},
        files={"file": ("photo.jpg", bytes_data, "image/jpeg")},
    )
    url = str(response.reply.SiteHostedPictureDetails.FullURL)
    log_debug(f"UploadSiteHostedPictures OK url={url}")
    return url
