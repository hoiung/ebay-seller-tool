"""Regression tests for _warn_missing_oauth_vars (Issue #5 Phase 2 / Stage 5 follow-up).

Stage 5 Layer 2 adversarial audit flagged this boot-time diagnostic function
as untested (server.py:244). Three behaviours must hold:

1. Silent return when all 3 runtime-required OAuth vars are present.
2. Warning emitted (exactly once) when any are missing, naming every
   missing var + every gated tool.
3. Fail-fast on first OAuth tool call is NOT masked — the function does
   not swallow or pre-catch the PermissionError raised by oauth.py:68 /
   browse.py:36. Verified indirectly: the function returns None in both
   branches, never raises.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import server


def _reload_warn() -> None:
    """Re-resolve the function reference off the reloaded server module."""
    importlib.reload(server)


def test_warn_silent_when_all_oauth_vars_present(capsys) -> None:
    env = {
        "EBAY_APP_CLIENT_ID": "x",
        "EBAY_APP_CLIENT_SECRET": "y",
        "EBAY_OWN_SELLER_USERNAME": "z",
    }
    with patch.dict("os.environ", env, clear=False):
        server._warn_missing_oauth_vars()
    out = capsys.readouterr()
    assert "OAuth env vars missing" not in out.err
    assert "OAuth env vars missing" not in out.out


def test_warn_fires_when_client_id_missing(capsys) -> None:
    env = {
        "EBAY_APP_CLIENT_ID": "",
        "EBAY_APP_CLIENT_SECRET": "y",
        "EBAY_OWN_SELLER_USERNAME": "z",
    }
    with patch.dict("os.environ", env, clear=False):
        server._warn_missing_oauth_vars()
    err = capsys.readouterr().err
    assert "OAuth env vars missing=EBAY_APP_CLIENT_ID" in err
    for tool in (
        "find_competitor_prices",
        "get_traffic_report",
        "get_listing_returns",
        "compute_return_rate",
    ):
        assert tool in err
    assert "fail-fast on first call preserved" in err


def test_warn_lists_all_three_when_all_missing(capsys) -> None:
    env = {
        "EBAY_APP_CLIENT_ID": "",
        "EBAY_APP_CLIENT_SECRET": "",
        "EBAY_OWN_SELLER_USERNAME": "",
    }
    with patch.dict("os.environ", env, clear=False):
        server._warn_missing_oauth_vars()
    err = capsys.readouterr().err
    for key in ("EBAY_APP_CLIENT_ID", "EBAY_APP_CLIENT_SECRET", "EBAY_OWN_SELLER_USERNAME"):
        assert key in err


def test_warn_never_raises() -> None:
    """Contract: additive diagnostic only; must not mask fail-fast downstream."""
    with patch.dict("os.environ", {"EBAY_APP_CLIENT_ID": ""}, clear=False):
        result = server._warn_missing_oauth_vars()
    assert result is None
