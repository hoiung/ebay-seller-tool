"""
Photo preprocessing + eBay Picture Services upload.

preprocess_for_ebay: one-pass validation + EXIF-transpose + RGB convert +
  ≤ 1600×1600 thumbnail + JPEG q90 optimize progressive — strips EXIF.
upload_one: wraps UploadSiteHostedPictures multipart for a single image,
  returns the eBay-hosted FullURL.

VISUAL_PHOTO_PATTERNS + glob_visual_photos: canonical disk-flow.sh visual-
photo glob (#25 triple-glob — `visual-*.png`, `SMART-*.png`,
`DISK-TEST-VISUAL-*.png`). Lifted out of server.py so audit scripts can
import without pulling FastMCP startup (M4 — Ralph deferred Opus dedup).

HEIC input is supported best-effort via pillow-heif (optional — iPhone users
only). Missing → ImportError swallowed at module import, HEIC paths then
fail inside Pillow's Image.open with its own clear error.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from ebay.client import execute_with_retry, log_debug

# SMART-test visual filenames per disk-flow.sh L534 + L544 (#25 stub-body
# correction A3): scope claimed `SMART-{serial}.png` but the writer emits
# `tests/visual-{serial}-{stamp}.png` AND `DISK-TEST-VISUAL-{serial}.png`
# at drive root. Glob all three for forward-compat — the union is small and
# bounded by the 24-photo PictureDetails cap downstream.
VISUAL_PHOTO_PATTERNS = ("visual-*.png", "SMART-*.png", "DISK-TEST-VISUAL-*.png")

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


def glob_visual_photos(folder: Path) -> list[str]:
    """Find SMART-test visual artefacts in a product folder (#25 triple-glob).

    Globs `visual-*.png`, `SMART-*.png`, and `DISK-TEST-VISUAL-*.png` — the
    three writer conventions documented in disk-flow.sh and dotfiles HARD
    CONTRACT. Stable order: pattern groups in declared sequence, files inside
    each group sorted alphabetically. De-duplicates against case-insensitive
    filesystems.

    Returns the file paths as strings so callers (server.py photo-upload flow,
    audit_smart_visuals.py) don't need to re-stringify.
    """
    if not folder.exists():
        return []
    seen: set[str] = set()
    results: list[str] = []
    for pattern in VISUAL_PHOTO_PATTERNS:
        for p in sorted(folder.glob(pattern)):
            s = str(p)
            if s in seen:
                continue
            seen.add(s)
            results.append(s)
    return results


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
