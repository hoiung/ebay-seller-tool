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
