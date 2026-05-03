"""Unit tests for ebay.listings.build_add_payload (P2.2, P2.3, P1.6)."""

from unittest.mock import patch

import pytest

from ebay.listings import build_add_payload

# Canonical 21-field ItemSpecifics sample (research §1.3)
SPECIFICS_21 = {
    "Brand": "Seagate",
    "MPN": "ST2000NX0253",
    "Model": "ST2000NX0253",
    "Product Line": "Enterprise Capacity",
    "Type": "Internal Hard Drive",
    "Drive Type(s) Supported": "HDD",
    "Storage Format": "HDD Only",
    "Storage Capacity": "2TB",
    "Interface": "SATA III",
    "Form Factor": "2.5 in",
    "Height": "15mm",
    "Rotation Speed": "7200 RPM",
    "Cache": "128 MB",
    "Transfer Rate": "6G",
    "Compatible With": "PC",
    "Features": ["Hot Swap", "24/7 Operation"],
    "Colour": "Silver",
    "Country of Origin": "China",
    "EAN": "Does not apply",
    "Manufacturer Warranty": "See Item Description",
    "Unit Type": "Unit",
}

VALID_UUID = "ABCDEF0123456789ABCDEF0123456789"


def _minimal(**overrides: object) -> dict:
    base = {
        "title": 'Seagate Enterprise Capacity 2TB 7200RPM 15mm 2.5" SATA III HDD ST2000NX0253',
        "description_html": "<html><body><h1>Drive</h1></body></html>",
        "price": 49.99,
        "quantity": 1,
        "condition_id": 3000,
        "condition_description": "SMART attributes within spec; no reallocated sectors.",
        "item_specifics": dict(SPECIFICS_21),
        "picture_urls": [
            "https://i.ebayimg.com/images/g/abc/$_57.JPG",
            "https://i.ebayimg.com/images/g/def/$_57.JPG",
        ],
        "uuid_hex": VALID_UUID,
    }
    base.update(overrides)
    return build_add_payload(**base)


def test_build_add_payload_full_payload_shape() -> None:
    """P2.2 — full field shape, matches issue spec verbatim."""
    payload = _minimal()
    item = payload["Item"]

    assert item["Quantity"] == "1"
    assert item["UUID"] == VALID_UUID
    assert item["StartPrice"]["@attrs"]["currencyID"] == "GBP"
    assert item["StartPrice"]["#text"] == "49.99"
    assert item["PictureDetails"]["PictureURL"] == [
        "https://i.ebayimg.com/images/g/abc/$_57.JPG",
        "https://i.ebayimg.com/images/g/def/$_57.JPG",
    ]
    assert item["ShippingDetails"]["GlobalShipping"] == "true"
    assert item["ShippingDetails"]["ShippingServiceOptions"]["FreeShipping"] == "true"
    assert (
        item["ShippingDetails"]["ShippingServiceOptions"]["ShippingServiceCost"]["#text"] == "0.00"
    )
    assert item["ReturnPolicy"]["ReturnsAcceptedOption"] == "ReturnsNotAccepted"
    assert item["ReturnPolicy"]["InternationalReturnsAcceptedOption"] == "ReturnsNotAccepted"
    assert item["Location"] == "Coventry"  # EBAY_SELLER_LOCATION from conftest
    assert item["PostalCode"] == "CV1 1AN"  # EBAY_SELLER_POSTCODE from conftest
    assert item["Country"] == "GB"
    assert item["Currency"] == "GBP"
    assert item["PrimaryCategory"]["CategoryID"] == "56083"
    assert item["ListingType"] == "FixedPriceItem"
    assert item["ListingDuration"] == "GTC"
    assert item["DispatchTimeMax"] == "3"
    assert item["PaymentMethods"] == []  # Managed Payments — empty list
    assert item["Description"].startswith("<![CDATA[")
    assert item["Description"].endswith("]]>")
    # ItemSpecifics serialised as NameValueList
    nvl_names = {row["Name"] for row in item["ItemSpecifics"]["NameValueList"]}
    assert "Brand" in nvl_names
    assert "MPN" in nvl_names


# ---- P2.3 boundary inputs ----


@pytest.mark.parametrize(
    "price,expected",
    [
        (49.9, "49.90"),
        (49, "49.00"),
        (0.01, "0.01"),
        (100.0, "100.00"),
        (55.5555, "55.56"),
    ],
)
def test_build_add_payload_price_stringified_two_dp(price: float, expected: str) -> None:
    payload = _minimal(price=price)
    assert payload["Item"]["StartPrice"]["#text"] == expected


@pytest.mark.parametrize(
    "bad_uuid",
    [
        "abcdef0123456789abcdef0123456789",  # lowercase
        "ABCDEF",  # too short
        "ABCDEF0123456789ABCDEF01234567890",  # too long
        "ABCDEFGH0123456789ABCDEF012345678",  # non-hex G,H
        "",
    ],
)
def test_build_add_payload_rejects_invalid_uuid(bad_uuid: str) -> None:
    with pytest.raises(ValueError, match=r"uuid_hex must match"):
        _minimal(uuid_hex=bad_uuid)


def test_build_add_payload_rejects_empty_picture_urls() -> None:
    with pytest.raises(ValueError, match=r"at least 1 URL"):
        _minimal(picture_urls=[])


def test_build_add_payload_rejects_25_picture_urls() -> None:
    urls = [f"https://i.ebayimg.com/images/g/{i:03d}/$_57.JPG" for i in range(25)]
    with pytest.raises(ValueError, match=r"at most 24 URLs"):
        _minimal(picture_urls=urls)


def test_build_add_payload_rejects_over_3975_joined_chars() -> None:
    # Generate enough URLs to exceed 3975 chars joined
    big_url = "https://i.ebayimg.com/" + "x" * 200 + "/$_57.JPG"
    urls = [big_url] * 20  # 20 * ~230 = ~4600 chars
    with pytest.raises(ValueError, match=r"total length \d+ chars exceeds"):
        _minimal(picture_urls=urls)


def test_build_add_payload_rejects_81_char_title() -> None:
    too_long = "X" * 81
    with pytest.raises(ValueError, match=r"exceeds 80-char"):
        _minimal(title=too_long)


def test_build_add_payload_accepts_80_char_title() -> None:
    ok = "X" * 80
    payload = _minimal(title=ok)
    assert payload["Item"]["Title"] == ok


def test_build_add_payload_missing_brand_raises_with_field_name() -> None:
    specs = dict(SPECIFICS_21)
    specs.pop("Brand")
    with pytest.raises(ValueError, match=r"'Brand'"):
        _minimal(item_specifics=specs)


def test_build_add_payload_missing_mpn_raises_with_field_name() -> None:
    specs = dict(SPECIFICS_21)
    specs.pop("MPN")
    with pytest.raises(ValueError, match=r"'MPN'"):
        _minimal(item_specifics=specs)


def test_build_add_payload_too_few_specifics_raises() -> None:
    specs = {"Brand": "Seagate", "MPN": "ST2000NX0253"}
    # Only 2 keys — well below 20
    with pytest.raises(ValueError, match=r"at least 20 keys"):
        _minimal(item_specifics=specs)


def test_build_add_payload_enforces_requires_quantity() -> None:
    # build_add_payload always calls _assert_requires_quantity internally.
    # Verify by passing a Quantity-bypass via custom monkeypatched path.
    with pytest.raises(ValueError, match=r"SAFETY: Add Quantity=0 < min=1"):
        _minimal(quantity=0)


def test_build_add_payload_never_invokes_no_quantity_assertion() -> None:
    """P1.6 — the Revise-path invariant must NOT fire on Add-path builds."""
    with patch("ebay.listings._assert_no_quantity") as mock_no_qty:
        _minimal()
    assert mock_no_qty.call_count == 0


def test_build_add_payload_cdata_wraps_description() -> None:
    html = "<html><body>test</body></html>"
    payload = _minimal(description_html=html)
    desc = payload["Item"]["Description"]
    assert desc == f"<![CDATA[{html}]]>"


def test_build_add_payload_features_list_preserved() -> None:
    payload = _minimal()
    features_row = next(
        row
        for row in payload["Item"]["ItemSpecifics"]["NameValueList"]
        if row["Name"] == "Features"
    )
    assert features_row["Value"] == ["Hot Swap", "24/7 Operation"]


def test_build_add_payload_custom_location_details_override_env() -> None:
    payload = _minimal(
        location_details={
            "Country": "US",
            "Location": "New York",
            "PostalCode": "10001",
            "Currency": "USD",
        }
    )
    assert payload["Item"]["Country"] == "US"
    assert payload["Item"]["Location"] == "New York"
    assert payload["Item"]["PostalCode"] == "10001"
    assert payload["Item"]["Currency"] == "USD"
    assert payload["Item"]["StartPrice"]["@attrs"]["currencyID"] == "USD"
