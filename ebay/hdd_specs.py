"""
Hand-curated HDD MPN catalogue keyed by OEM model.

Seeded from the 22 live listings exported to /tmp/ebay-listings-live.json as a
one-off data read. Keys are the OEM model as it appears on the physical drive
label (not HPE option / spare numbers). `-Series-Beta` suffix distinguishes the Series-Beta
label variant of a shared MPN (Fabrikam relabelled some drives without changing
the underlying OEM MPN).

Consumed by server.py::create_listing (P3.4) to fill in spec values that are
not printed on the drive label (cache, family, height). Miss on OEM MPN here
forces the operator to add a new row before the listing can be created — this
is the anti-"silent default" guard.

Required sub-keys: brand, family, capacity, rpm, interface, transfer_rate,
cache, form_factor, height. `height` may be None for 3.5" drives only (they
have no short/tall variant).
"""

HDD_SPECS: dict[str, dict[str, str | None]] = {
    "MDL-A01": {
        "brand": "Fabrikam",
        "family": "Series-Alpha",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A02": {
        "brand": "Fabrikam",
        "family": "Series-Alpha",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SAS",
        "transfer_rate": "12G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A03": {
        "brand": "Fabrikam",
        "family": "Series-Alpha",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A03-VAR": {
        "brand": "Fabrikam",
        "family": "Series-Beta-7X2000",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A04": {
        "brand": "Fabrikam",
        "family": "Series-Beta-7X2000",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A05": {
        "brand": "Fabrikam",
        "family": "Series-Alpha",
        "capacity": "4TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Fabrikam Series-Gamma 2.5" Product Manual 100807728a (MDL-A07 + MDL-A06)
    # https://www.Fabrikam.com/www-content/product-content/Fabrikam-laptop-fam/series-gamma_25/en-us/docs/100807728a.pdf
    # Series-Gamma 2.5" family is 5400 RPM SMR (laptop/NAS), SATA 6Gb/s, 128 MB cache.
    # MDL-A06 = 4TB variant at 15mm z-height; MDL-A07 = 2TB variant at 7mm z-height.
    "MDL-A06": {
        "brand": "Fabrikam",
        "family": "Series-Gamma",
        "capacity": "4TB",
        "rpm": "5400 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A07": {
        "brand": "Fabrikam",
        "family": "Series-Gamma",
        "capacity": "2TB",
        "rpm": "5400 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "7mm",
    },
    "MDL-A08": {
        "brand": "Fabrikam",
        "family": "Series-Delta-3",
        "capacity": "3TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MDL-A09": {
        "brand": "Fabrikam",
        "family": "Series-Delta",
        "capacity": "1TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MDL-A10": {
        "brand": "Fabrikam",
        "family": "Series-Epsilon",
        "capacity": "1.2TB",
        "rpm": "10000 RPM",
        "interface": "SAS",
        "transfer_rate": "12G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A11": {
        "brand": "Wingtip",
        "family": "Series-Zeta-4000",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MDL-A12": {
        "brand": "Wingtip",
        "family": "Series-Zeta-4000",
        "capacity": "3TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MDL-A13": {
        "brand": "Wingtip",
        "family": "Series-Zeta-6000",
        "capacity": "4TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MDL-A14": {
        "brand": "Contoso",
        "family": "Series-MGA",
        "capacity": "4TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MDL-A15": {
        "brand": "Contoso",
        "family": "MDL-A15",
        "capacity": "900GB",
        "rpm": "10000 RPM",
        "interface": "SAS",
        "transfer_rate": "12G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "MDL-A16": {
        "brand": "Wingtip",
        "family": "Series-Zeta-C600",
        "capacity": "300GB",
        "rpm": "10000 RPM",
        "interface": "SAS",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    # --- 2026-06-07 NEW/sealed batch (HPE-rebadged enterprise pulls) ---
    # Source: Fabrikam Series-Delta-3 datasheet DS1769.1-1210US (MDL-A17 =
    # 1TB SATA 6Gb/s 7.2K, 128MB, 3.5", 512n). HPE option HPN-001 / spare
    # HPN-002 (HPN-008 + HPN-009).
    "MDL-A17": {
        "brand": "Fabrikam",
        "family": "Series-Delta-3",
        "capacity": "1TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Contoso MG03ACA series product overview (MDL-A18 = 1TB SATA
    # 6Gb/s 7.2K, 64MiB, 3.5", 512n). HPE option HPN-001 (HPE HPN-010).
    "MDL-A18": {
        "brand": "Contoso",
        "family": "Series-MGB",
        "capacity": "1TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Litware RE4 datasheet 2178-771114 (MDL-A19 = 500GB SATA 3Gb/s 7.2K,
    # 64MB, 3.5"). HPE option HPN-003 / spare HPN-004 (HPE HPN-011).
    "MDL-A19": {
        "brand": "Litware",
        "family": "RE4",
        "capacity": "500GB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "3G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Fabrikam Series-Delta Product Manual 100516232f (MDL-A20 =
    # 500GB SATA 3Gb/s 7.2K, 32MB, 3.5", 512n). HP option HPN-003 / spare
    # HPN-004 (HP HPN-012, PartSurfer P/N HPN-005).
    "MDL-A20": {
        "brand": "Fabrikam",
        "family": "Series-Delta",
        "capacity": "500GB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "3G",
        "cache": "32 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Fabrikam Series-Delta-2 datasheet DS1719.4-1207 (MDL-A21 =
    # 500GB SATA 6Gb/s 7.2K, 64MB, 2.5", 15mm z-height, 512n). HPE markets it as
    # a 3G midline part; drive is natively 6Gb/s. HPE option HPN-006 / spare
    # HPN-007 (HPE HPN-013). 15mm height — needs the 15mm warning.
    "MDL-A21": {
        "brand": "Fabrikam",
        "family": "Series-Delta-2",
        "capacity": "500GB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
}
