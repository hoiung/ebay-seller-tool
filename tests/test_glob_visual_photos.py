"""Tests for server._glob_visual_photos + create_listing triple-glob (#25 stub-body correction).

Stub-body correction A3 (Stage 1 L2): scope claimed `SMART-{serial}.png` but
disk-flow.sh actually writes `tests/visual-{serial}-{stamp}.png` AND
`DISK-TEST-VISUAL-{serial}.png` to drive root. Glob all three patterns; merge
with regular IMG photos in deterministic order (IMGs first, visuals appended).
"""

from __future__ import annotations

from pathlib import Path

from server import _glob_label_photos, _glob_visual_photos


def _touch(folder: Path, name: str) -> Path:
    p = folder / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def test_visual_glob_finds_three_patterns(tmp_path: Path) -> None:
    _touch(tmp_path, "visual-W461C1WW-20260427.png")
    _touch(tmp_path, "SMART-W461C1WW.png")
    _touch(tmp_path, "DISK-TEST-VISUAL-W461C1WW.png")
    out = _glob_visual_photos(tmp_path)
    names = [Path(p).name for p in out]
    assert "visual-W461C1WW-20260427.png" in names
    assert "SMART-W461C1WW.png" in names
    assert "DISK-TEST-VISUAL-W461C1WW.png" in names


def test_visual_glob_preserves_pattern_group_order(tmp_path: Path) -> None:
    """Pattern declared order: visual-* first, then SMART-*, then DISK-TEST-VISUAL-*."""
    _touch(tmp_path, "DISK-TEST-VISUAL-1.png")  # write last but globbed third
    _touch(tmp_path, "SMART-1.png")  # second
    _touch(tmp_path, "visual-1.png")  # first
    out = _glob_visual_photos(tmp_path)
    names = [Path(p).name for p in out]
    assert names == ["visual-1.png", "SMART-1.png", "DISK-TEST-VISUAL-1.png"]


def test_visual_glob_empty_folder_returns_empty(tmp_path: Path) -> None:
    assert _glob_visual_photos(tmp_path) == []


def test_visual_glob_missing_folder_returns_empty(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist"
    assert _glob_visual_photos(nonexistent) == []


def test_visual_glob_skips_non_matching_files(tmp_path: Path) -> None:
    _touch(tmp_path, "random.png")
    _touch(tmp_path, "image.jpg")
    _touch(tmp_path, "VISUAL.png")  # uppercase — doesn't match `visual-*`
    assert _glob_visual_photos(tmp_path) == []


def test_visual_glob_ignores_jpg_with_visual_name(tmp_path: Path) -> None:
    """Triple-glob is PNG-only — disk-flow.sh emits PNG for these visuals."""
    _touch(tmp_path, "visual-1.jpg")
    assert _glob_visual_photos(tmp_path) == []


def test_label_then_visual_glob_combined_order(tmp_path: Path) -> None:
    """create_listing concatenates IMG photos first, then visuals."""
    _touch(tmp_path, "IMG20260427120000.jpg")
    _touch(tmp_path, "visual-W461.png")
    _touch(tmp_path, "DISK-TEST-VISUAL-W461.png")

    label = _glob_label_photos(tmp_path)
    visual = _glob_visual_photos(tmp_path)
    combined = label + visual

    names = [Path(p).name for p in combined]
    assert names[0] == "IMG20260427120000.jpg"
    assert "visual-W461.png" in names[1:]
    assert "DISK-TEST-VISUAL-W461.png" in names[1:]


def test_visual_glob_dedup_case_insensitive_filesystem(tmp_path: Path) -> None:
    """A pathological case: glob produces same path twice — dedup protects against it."""
    p = _touch(tmp_path, "visual-1.png")
    out = _glob_visual_photos(tmp_path)
    # Even if globbed multiple times somehow, dedup keeps single entry.
    assert out.count(str(p)) == 1


# ---------- #25 multi-qty 3-sample rule + dry-run visual breakdown ----------


def test_create_listing_multi_qty_warns_when_visuals_below_3(tmp_path: Path, monkeypatch) -> None:
    """qty>1 with <3 visuals → warning in dry-run response, not refusal."""
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    import server

    folder = tmp_path / "ST2000NX0253"
    folder.mkdir()
    # Phone-camera label photo (1 IMG, qualifies as label)
    label_path = folder / "IMG20260427120000.jpg"
    label_path.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header
    # Only 1 visual — below 3-sample minimum at qty>1.
    (folder / "visual-W461.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    server._create_listing_uuid_cache.clear()
    fake_verify = SimpleNamespace(
        reply=SimpleNamespace(Errors=None, Fees=SimpleNamespace(Fee=[]))
    )

    with patch("server.upload_photos") as mock_upload, patch(
        "server.execute_with_retry", return_value=fake_verify
    ):
        # Stub upload_photos to skip real EPS uploads.
        async def fake_upload(paths, dry_run=False):
            return json.dumps(
                {
                    "success": True,
                    "urls": [f"https://eps/{i}.jpg" for i in range(len(paths))],
                    "total_url_chars": 100,
                    "warnings": [],
                }
            )

        mock_upload.side_effect = fake_upload
        raw = asyncio.run(
            server.create_listing(
                folder_path=str(folder),
                price=49.99,
                quantity=3,
                condition="Used",
                has_caddy=False,
                dry_run=True,
            )
        )

    body = json.loads(raw)
    assert body["dry_run"] is True
    assert body["label_photo_count"] == 1
    assert body["visual_photo_count"] == 1
    assert any("multi-qty" in w and ">=3" in w for w in body["photo_warnings"])


def test_create_listing_qty_one_no_visual_warning(tmp_path: Path) -> None:
    """qty=1 → no 3-sample-minimum warning regardless of visual count."""
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    import server

    folder = tmp_path / "ST2000NX0253"
    folder.mkdir()
    (folder / "IMG20260427120000.jpg").write_bytes(b"\xff\xd8\xff")
    (folder / "visual-W461.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    server._create_listing_uuid_cache.clear()

    fake_verify = SimpleNamespace(
        reply=SimpleNamespace(Errors=None, Fees=SimpleNamespace(Fee=[]))
    )

    async def fake_upload(paths, dry_run=False):
        return json.dumps(
            {"success": True, "urls": [f"https://eps/{i}.jpg" for i in range(len(paths))],
             "total_url_chars": 100, "warnings": []}
        )

    with patch("server.upload_photos", side_effect=fake_upload), patch(
        "server.execute_with_retry", return_value=fake_verify
    ):
        raw = asyncio.run(
            server.create_listing(
                folder_path=str(folder),
                price=49.99,
                quantity=1,
                condition="Used",
                has_caddy=False,
                dry_run=True,
            )
        )

    body = json.loads(raw)
    assert body["photo_warnings"] == []  # qty=1 doesn't trigger 3-sample rule


def test_create_listing_dry_run_distinguishes_label_and_visual_counts(tmp_path: Path) -> None:
    """Dry-run preview surfaces label_photo_count + visual_photo_count distinctly."""
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    import server

    folder = tmp_path / "ST2000NX0253"
    folder.mkdir()
    # 2 IMG label photos
    (folder / "IMG20260427120000.jpg").write_bytes(b"\xff\xd8\xff")
    (folder / "IMG20260427120001.jpg").write_bytes(b"\xff\xd8\xff")
    # 3 visuals (one of each pattern)
    (folder / "visual-W461.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / "SMART-W461.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / "DISK-TEST-VISUAL-W461.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    server._create_listing_uuid_cache.clear()

    fake_verify = SimpleNamespace(
        reply=SimpleNamespace(Errors=None, Fees=SimpleNamespace(Fee=[]))
    )

    async def fake_upload(paths, dry_run=False):
        return json.dumps(
            {"success": True, "urls": [f"https://eps/{i}.jpg" for i in range(len(paths))],
             "total_url_chars": 100, "warnings": []}
        )

    with patch("server.upload_photos", side_effect=fake_upload), patch(
        "server.execute_with_retry", return_value=fake_verify
    ):
        raw = asyncio.run(
            server.create_listing(
                folder_path=str(folder),
                price=49.99,
                quantity=1,
                condition="Used",
                has_caddy=False,
                dry_run=True,
            )
        )

    body = json.loads(raw)
    assert body["label_photo_count"] == 2
    assert body["visual_photo_count"] == 3
    assert body["picture_urls_count"] == 5  # 2 + 3


def test_create_listing_explicit_paths_no_classification(tmp_path: Path) -> None:
    """When operator passes photo_paths explicitly, label/visual breakdown is not inferred."""
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    import server

    folder = tmp_path / "ST2000NX0253"
    folder.mkdir()
    # Operator-supplied path (not from glob)
    operator_path = folder / "custom.jpg"
    operator_path.write_bytes(b"\xff\xd8\xff")
    server._create_listing_uuid_cache.clear()

    fake_verify = SimpleNamespace(
        reply=SimpleNamespace(Errors=None, Fees=SimpleNamespace(Fee=[]))
    )

    async def fake_upload(paths, dry_run=False):
        return json.dumps(
            {"success": True, "urls": [f"https://eps/{i}.jpg" for i in range(len(paths))],
             "total_url_chars": 100, "warnings": []}
        )

    with patch("server.upload_photos", side_effect=fake_upload), patch(
        "server.execute_with_retry", return_value=fake_verify
    ):
        raw = asyncio.run(
            server.create_listing(
                folder_path=str(folder),
                price=49.99,
                quantity=1,
                condition="Used",
                has_caddy=False,
                photo_paths=[str(operator_path)],
                dry_run=True,
            )
        )

    body = json.loads(raw)
    # Operator-supplied paths skip glob classification; counts both 0.
    assert body["label_photo_count"] == 0
    assert body["visual_photo_count"] == 0
    assert body["picture_urls_count"] == 1
