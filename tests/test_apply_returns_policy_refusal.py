"""Stage 5 A2 Layer-5 gap closure — apply_returns_policy.py hard-refusal coverage.

Per the 2026-05-26 permanent fix, `scripts/apply_returns_policy.py` is fenced
off behind an explicit operator-acknowledged flag. Reverting the refusal
without flipping these tests would silently re-enable the historical
Business-Policies enrolment migration that destroyed inline shipping
3× historically (see feedback_ebay_default_shipping_poisoned.md).

We invoke the script via subprocess so the `__main__` block (which contains
the refusal gate) actually fires — `importlib` would skip it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "apply_returns_policy.py"


def test_apply_returns_policy_refuses_without_apply_flag() -> None:
    """No --apply → script should print refusal/usage and exit non-zero
    (argparse default behaviour for required flag-less invocation in
    --apply mode + the explicit refusal block)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0, (
        f"Script must refuse without --apply (got exit {result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_apply_returns_policy_refuses_without_ack_flag() -> None:
    """--apply WITHOUT --i-acknowledge-shipping-corruption → hard-refusal
    block fires (exit 2). This is the central defense layer; if it stops
    firing, the historical migration script can be run again and the bug
    returns. The 3-incident history is documented in the script's
    _OBSOLETE_REFUSAL banner."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--apply"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 2, (
        f"Hard-refusal must exit 2 without ack flag "
        f"(got exit {result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = (result.stderr or "") + (result.stdout or "")
    # Refusal banner mentions shipping corruption / historical incidents
    assert "shipping" in combined.lower() or "obsolete" in combined.lower(), (
        f"Refusal banner must surface shipping/obsolete keyword. "
        f"Got combined output:\n{combined}"
    )


def test_apply_returns_policy_help_does_not_require_env() -> None:
    """`--help` must work without the legacy Business-Policies env vars set.
    Validates that the import path doesn't fail at module-load time even
    when EBAY_*_PROFILE_ID are absent (the helper raising NotImplementedError
    only fires inside main(), not at import)."""
    env_override = {
        k: v
        for k, v in __import__("os").environ.items()
        if not k.startswith("EBAY_PAYMENT_PROFILE_ID")
        and not k.startswith("EBAY_SHIPPING_PROFILE_ID")
        and not k.startswith("EBAY_RETURN_PROFILE_ID")
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env_override,
    )
    assert result.returncode == 0, (
        f"--help must exit 0 (got {result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
