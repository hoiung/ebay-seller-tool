"""Shared test fixtures.

Sets dummy eBay env vars so modules under test import cleanly without requiring
a real .env file. Importing ``server`` must make NO network call — that
invariant is enforced by ``tests/test_no_boot_network.py``, which monkeypatches
``ebay.client.execute_with_retry`` + ``socket.socket`` and asserts that
importing/reloading server invokes neither (the #40 AC1.2 boot-call fix; the
old "no network calls are ever made" prose here was unenforced). Do not
reinstate any module-level Trading-API call.
"""

import os
from pathlib import Path

# Point the loader at the SYNTHETIC example data set so the public suite is
# deterministic regardless of any real EBAY_LISTING_DATA_DIR a developer may
# have exported (the public repo ships no product data — see
# ebay/catalogue_loader.py). Forced (not setdefault) so test assertions on the
# synthetic contract never see a developer's real overlay. Fail-loud tests that
# need the env UNSET use monkeypatch.delenv.
os.environ["EBAY_LISTING_DATA_DIR"] = str(
    Path(__file__).resolve().parent.parent / "ebay" / "listing_data.example"
)

os.environ.setdefault("EBAY_APP_ID", "test-app-id")
os.environ.setdefault("EBAY_CERT_ID", "test-cert-id")
os.environ.setdefault("EBAY_DEV_ID", "test-dev-id")
os.environ.setdefault("EBAY_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("EBAY_SITE_ID", "3")
os.environ.setdefault("EBAY_SELLER_LOCATION", "Coventry")
os.environ.setdefault("EBAY_SELLER_POSTCODE", "CV1 1AN")
# Legacy Business Policies env vars (issue #29). Post 2026-05-26 permanent
# fix, build_add_payload + build_revise_payload no longer require these —
# _REQUIRED_SELLER_PROFILE_ENV_VARS is the empty tuple and SellerProfiles is
# never attached to any payload. Values are set via setdefault solely for
# any legacy fixture / older test that still reads them; new tests should
# not depend on these vars.
os.environ.setdefault("EBAY_PAYMENT_PROFILE_ID", "100000000001")
os.environ.setdefault("EBAY_SHIPPING_PROFILE_ID", "100000000002")
os.environ.setdefault("EBAY_RETURN_PROFILE_ID", "100000000003")
