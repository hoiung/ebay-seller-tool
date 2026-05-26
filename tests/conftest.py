"""Shared test fixtures.

Sets dummy eBay env vars so modules under test import cleanly without
requiring a real .env file. No network calls are ever made by unit tests —
the Trading API is always mocked.
"""

import os

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
