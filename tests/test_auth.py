"""#40 AC5.4 — test seam for the startup credential gate (ebay/auth.py).

validate_credentials (the boot fail-fast on missing env) + check_token_expiry
(best-effort token hygiene) previously had no direct coverage.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ebay import auth


def test_validate_credentials_passes_when_all_set() -> None:
    # conftest sets every REQUIRED_VARS entry via setdefault — no raise expected.
    auth.validate_credentials()


@pytest.mark.parametrize("missing_var", auth.REQUIRED_VARS)
def test_validate_credentials_raises_systemexit_when_var_missing(
    monkeypatch: pytest.MonkeyPatch, missing_var: str
) -> None:
    monkeypatch.delenv(missing_var, raising=False)
    with pytest.raises(SystemExit) as exc:
        auth.validate_credentials()
    assert exc.value.code == 1


def test_check_token_expiry_calls_gettokenstatus() -> None:
    fake = SimpleNamespace(
        reply=SimpleNamespace(
            TokenStatus=SimpleNamespace(ExpirationTime="2030-01-01T00:00:00.000Z", Status="Active")
        )
    )
    with patch("ebay.client.execute_with_retry", return_value=fake) as mock_exec:
        auth.check_token_expiry()
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0] == "GetTokenStatus"


def test_check_token_expiry_swallows_network_error() -> None:
    # Best-effort startup hygiene — a network failure must NOT block startup.
    with patch("ebay.client.execute_with_retry", side_effect=RuntimeError("boom")):
        auth.check_token_expiry()  # no exception propagates
