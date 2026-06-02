"""#40 AC5.3 — permanently enforce the AC1.2 boot-call fix.

Importing / reloading ``server`` must make ZERO eBay network calls. The guard is
a socket + execute_with_retry sentinel (NOT a stderr-string grep). If a
module-level Trading-API call is ever reinstated (the R-BUG2 regression),
reloading server trips the counter and this test fails.
"""

from __future__ import annotations

import importlib
import socket as socket_mod

import server
from ebay import client as client_mod


def test_importing_server_makes_no_network_call(monkeypatch) -> None:
    counts = {"exec": 0, "socket": 0}

    def _sentinel_exec(*_args, **_kwargs):
        # Count BEFORE raising — check_token_expiry swallows exceptions, so the
        # counter (not propagation) is what proves a boot call happened.
        counts["exec"] += 1
        raise RuntimeError("no eBay network call is permitted at import time")

    real_socket = socket_mod.socket

    def _sentinel_socket(*args, **kwargs):
        counts["socket"] += 1
        return real_socket(*args, **kwargs)

    monkeypatch.setattr(client_mod, "execute_with_retry", _sentinel_exec)
    monkeypatch.setattr(socket_mod, "socket", _sentinel_socket)
    try:
        importlib.reload(server)
        assert counts["exec"] == 0, (
            "importing server called execute_with_retry — boot-time network regressed (AC1.2)"
        )
        assert counts["socket"] == 0, (
            "importing server opened a socket — boot-time network regressed (AC1.2)"
        )
    finally:
        # Restore a clean server module (real bindings) for downstream tests.
        monkeypatch.undo()
        importlib.reload(server)
