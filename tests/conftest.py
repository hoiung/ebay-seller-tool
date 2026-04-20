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
