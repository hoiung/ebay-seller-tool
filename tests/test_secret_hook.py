"""Test the public-repo secret detection hook (Issue #4 AC 4.5).

Invokes scripts/check-public-repo-secrets.py via subprocess against
temp files to confirm:
  (a) existing `itdirectuk*` blocklist still fires
  (b) new EBAY_OAUTH_REFRESH_TOKEN / EBAY_APP_CLIENT_SECRET value literals
      are caught by the generic secret-assignment pattern
  (c) a clean file passes with exit 0
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check-public-repo-secrets.py"


def _run_hook(target_path: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["python3", str(_SCRIPT), str(target_path)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_seller_username_leak_blocked(tmp_path: Path) -> None:
    """Blocklist catches the store username literal."""
    bad = tmp_path / "bad.py"
    bad.write_text('STORE = "itdirectuk888"\n')
    code, out, err = _run_hook(bad)
    assert code != 0
    combined = (out + err).lower()
    assert "itdirectuk" in combined or "blocklist" in combined


def test_refresh_token_value_blocked(tmp_path: Path) -> None:
    """Generic-secret pattern catches an EBAY_OAUTH_REFRESH_TOKEN=... literal."""
    bad = tmp_path / "bad_token.py"
    # Literal that would fire a real-world secret sniffer — use a plausible-looking
    # refresh_token assignment. Not a real token.
    bad.write_text('EBAY_OAUTH_REFRESH_TOKEN = "v^1.1#i^1#f^0#p^3#I^3#r^1#t^Ul41Xz"\n')
    code, out, err = _run_hook(bad)
    assert code != 0


def test_client_secret_value_blocked(tmp_path: Path) -> None:
    """Generic-secret pattern catches EBAY_APP_CLIENT_SECRET=... literal."""
    bad = tmp_path / "bad_secret.py"
    bad.write_text('EBAY_APP_CLIENT_SECRET = "SBX-a1b2c3d4e5f6g7h8i9j0"\n')
    code, out, err = _run_hook(bad)
    assert code != 0


def test_clean_file_passes(tmp_path: Path) -> None:
    """A file with only env-var references + no literals passes."""
    good = tmp_path / "clean.py"
    good.write_text(
        "import os\n"
        "print(os.environ.get('EBAY_OAUTH_REFRESH_TOKEN', 'unset'))\n"
        "print(os.environ['EBAY_APP_CLIENT_ID'])\n"
    )
    code, out, err = _run_hook(good)
    # Must exit 0 — no literals to flag.
    assert code == 0, f"Expected exit 0, got {code}. stdout={out!r} stderr={err!r}"
