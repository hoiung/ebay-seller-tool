"""Daily Trading API call accountant (#12 Phase 2.1).

Persistent counter for eBay Trading API verbs. Written to
``~/.local/share/ebay-seller-tool/api-calls-{YYYYMMDD}.json`` after every
successful ``execute_with_retry`` invocation (wired at ``client.py:134``).

The 5000/day Trading API limit is FLAT across all verbs — no per-verb
subdivision documented (per Stage 1 finding A4 in #12 research). Caps live
in ``CALL_CAPS`` for forward-compatibility if eBay introduces per-verb caps
later.

Concurrency model: ``fcntl.flock`` with a 30s timeout protects each daily
file from racing CLI invocations + the MCP server. Atomic write via
``tempfile + os.fsync + os.rename`` keeps the on-disk file consistent if
the process crashes mid-update.

Retention: 30-day rolling window. First call of each calendar day prunes
files older than 30 days; the per-day marker ``.pruned-today`` short-
circuits subsequent calls so the prune scan runs at most once per day.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CALL_CAPS: dict[str, int] = {
    "_default": 5000,
    "AddMemberMessageRTQ": 5000,
}

_STATE_DIR = Path.home() / ".local" / "share" / "ebay-seller-tool"
_RETENTION_DAYS = 30
# Stage 5 fix L1.J — was 30s. The accountant fires from the Trading API hot
# path (every successful execute_with_retry), and a stuck process holding the
# flock would block the API caller for up to 30s. 5s keeps tail latency well
# below MAX_CUMULATIVE_TIMEOUT_SECONDS=15s in client.py while still tolerating
# normal contention (uncontended record_call ~4ms; 4-worker contended ~2ms).
_LOCK_TIMEOUT_SECONDS = 5
_PRUNE_MARKER = "_pruned"  # field on today's file marking the prune ran


class CallAccountantError(RuntimeError):
    """Raised on lock-acquire timeout or unrecoverable IO error."""


def _today_yyyymmdd() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _file_for(yyyymmdd: str) -> Path:
    return _STATE_DIR / f"api-calls-{yyyymmdd}.json"


def _ensure_state_dir() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _acquire_lock(fd: int, timeout_seconds: int = _LOCK_TIMEOUT_SECONDS) -> None:
    """Block-acquire an exclusive flock with a wall-clock timeout."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            if time.monotonic() >= deadline:
                raise CallAccountantError(
                    f"call_accountant: flock timeout after {timeout_seconds}s — "
                    f"another process is holding the daily counter"
                )
            time.sleep(0.1)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to ``path`` via tempfile + fsync + rename. Same-dir tempfile
    so the rename is atomic on POSIX."""
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _read_counts(path: Path) -> dict:
    """Read the daily counts file. Returns {} on missing-or-corrupted (fail-
    soft per AP #12 — corrupted state must not block live API calls)."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def _maybe_prune(today: str) -> None:
    """Delete daily counter files older than ``_RETENTION_DAYS`` days.

    Uses an in-file marker on today's counter to short-circuit so the prune
    runs at most once per calendar day. Fail-soft on filesystem errors.
    """
    today_path = _file_for(today)
    counts = _read_counts(today_path)
    if counts.get(_PRUNE_MARKER):
        return
    cutoff_date = datetime.now(timezone.utc).date() - timedelta(days=_RETENTION_DAYS)
    try:
        for f in _STATE_DIR.iterdir():
            name = f.name
            if not (name.startswith("api-calls-") and name.endswith(".json")):
                continue
            stamp = name[len("api-calls-"):-len(".json")]
            try:
                file_date = datetime.strptime(stamp, "%Y%m%d").date()
            except ValueError:
                continue
            if file_date < cutoff_date:
                f.unlink(missing_ok=True)
    except OSError:
        return
    counts[_PRUNE_MARKER] = today
    _atomic_write_json(today_path, counts)


def record_call(call_name: str) -> None:
    """Increment today's counter for ``call_name``.

    Idempotency note: this does NOT dedup. Every call to ``record_call``
    bumps the counter by one. The send-side workflow consumes the
    accountant only after a successful eBay response (sentinel-based
    idempotency at the higher level prevents double-recording on crash-
    retry — the eBay call has already been recorded by the prior process).
    """
    if not call_name or not isinstance(call_name, str):
        raise ValueError(f"call_name must be a non-empty string, got {call_name!r}")
    _ensure_state_dir()
    today = _today_yyyymmdd()
    path = _file_for(today)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire_lock(fd)
        counts = _read_counts(path)
        counts[call_name] = int(counts.get(call_name, 0)) + 1
        _atomic_write_json(path, counts)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    _maybe_prune(today)


def today_count(call_name: str) -> int:
    """Return today's count for ``call_name``. 0 on missing-file or corrupted
    state (fail-soft)."""
    if not call_name or not isinstance(call_name, str):
        raise ValueError(f"call_name must be a non-empty string, got {call_name!r}")
    counts = _read_counts(_file_for(_today_yyyymmdd()))
    value = counts.get(call_name, 0)
    return int(value) if isinstance(value, (int, str)) and str(value).lstrip("-").isdigit() else 0


def daily_budget_remaining(call_name: str, daily_cap: int | None = None) -> int:
    """Return remaining quota for ``call_name`` against the daily cap.

    ``daily_cap`` precedence: explicit arg > ``CALL_CAPS[call_name]`` >
    ``CALL_CAPS["_default"]`` (5000). May return negative when the counter
    has overshot the cap (operator-visible signal, never silently floored).
    """
    cap = daily_cap if daily_cap is not None else CALL_CAPS.get(call_name, CALL_CAPS["_default"])
    return cap - today_count(call_name)
