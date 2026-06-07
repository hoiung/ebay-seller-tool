"""
Hand-curated HDD MPN catalogue keyed by OEM model.

Seeded from the 22 live listings exported to /tmp/ebay-listings-live.json as a
one-off data read. Keys are the OEM model as it appears on the physical drive
label (not HPE option / spare numbers). `-EXOS` suffix distinguishes the Exos
label variant of a shared MPN (Seagate relabelled some drives without changing
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
    "ST2000NX0303": {
        "brand": "Seagate",
        "family": "Enterprise Capacity",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "ST2000NX0273": {
        "brand": "Seagate",
        "family": "Enterprise Capacity",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SAS",
        "transfer_rate": "12G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "ST2000NX0253": {
        "brand": "Seagate",
        "family": "Enterprise Capacity",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "ST2000NX0253-EXOS": {
        "brand": "Seagate",
        "family": "Exos 7E2000",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "ST2000NX0403": {
        "brand": "Seagate",
        "family": "Exos 7E2000",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "ST4000NM0035": {
        "brand": "Seagate",
        "family": "Enterprise Capacity",
        "capacity": "4TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Seagate BarraCuda 2.5" Product Manual 100807728a (ST2000LM015 + ST4000LM016)
    # https://www.seagate.com/www-content/product-content/seagate-laptop-fam/barracuda_25/en-us/docs/100807728a.pdf
    # BarraCuda 2.5" family is 5400 RPM SMR (laptop/NAS), SATA 6Gb/s, 128 MB cache.
    # ST4000LM016 = 4TB variant at 15mm z-height; ST2000LM015 = 2TB variant at 7mm z-height.
    "ST4000LM016": {
        "brand": "Seagate",
        "family": "BarraCuda",
        "capacity": "4TB",
        "rpm": "5400 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "ST2000LM015": {
        "brand": "Seagate",
        "family": "BarraCuda",
        "capacity": "2TB",
        "rpm": "5400 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "7mm",
    },
    "ST3000NM0033": {
        "brand": "Seagate",
        "family": "Constellation ES.3",
        "capacity": "3TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MB1000GCEHH": {
        "brand": "Seagate",
        "family": "Constellation ES",
        "capacity": "1TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "EG1200JEMDA": {
        "brand": "Seagate",
        "family": "Enterprise Performance 10K.8",
        "capacity": "1.2TB",
        "rpm": "10000 RPM",
        "interface": "SAS",
        "transfer_rate": "12G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "HUS724020ALA640": {
        "brand": "HGST",
        "family": "Ultrastar 7K4000",
        "capacity": "2TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "HUS724030ALA640": {
        "brand": "HGST",
        "family": "Ultrastar 7K4000",
        "capacity": "3TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "HUS726040ALA614": {
        "brand": "HGST",
        "family": "Ultrastar 7K6000",
        "capacity": "4TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "MG04ACA400N": {
        "brand": "Toshiba",
        "family": "MG04 Series",
        "capacity": "4TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    "AL14SEB090N": {
        "brand": "Toshiba",
        "family": "AL14SE",
        "capacity": "900GB",
        "rpm": "10000 RPM",
        "interface": "SAS",
        "transfer_rate": "12G",
        "cache": "128 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    "HUC101030CSS600": {
        "brand": "HGST",
        "family": "Ultrastar C10K600",
        "capacity": "300GB",
        "rpm": "10000 RPM",
        "interface": "SAS",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
    # --- 2026-06-07 NEW/sealed batch (HPE-rebadged enterprise pulls) ---
    # Source: Seagate Constellation ES.3 datasheet DS1769.1-1210US (ST1000NM0033 =
    # 1TB SATA 6Gb/s 7.2K, 128MB, 3.5", 512n). HPE option 657750-B21 / spare
    # 657739-001 (MB1000GCWCV + MD1000GCWCV).
    "ST1000NM0033": {
        "brand": "Seagate",
        "family": "Constellation ES.3",
        "capacity": "1TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "128 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Toshiba MG03ACA series product overview (MG03ACA100 = 1TB SATA
    # 6Gb/s 7.2K, 64MiB, 3.5", 512n). HPE option 657750-B21 (HPE MB1000GDUNU).
    "MG03ACA100": {
        "brand": "Toshiba",
        "family": "MG03 Series",
        "capacity": "1TB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: WD RE4 datasheet 2178-771114 (WD5003ABYX = 500GB SATA 3Gb/s 7.2K,
    # 64MB, 3.5"). HPE option 458928-B21 / spare 459319-001 (HPE MB0500EBNCR).
    "WD5003ABYX": {
        "brand": "Western Digital",
        "family": "RE4",
        "capacity": "500GB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "3G",
        "cache": "64 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Seagate Constellation ES Product Manual 100516232f (ST3500514NS =
    # 500GB SATA 3Gb/s 7.2K, 32MB, 3.5", 512n). HP option 458928-B21 / spare
    # 459319-001 (HP MB0500EAMZD, PartSurfer P/N 507631-001).
    "ST3500514NS": {
        "brand": "Seagate",
        "family": "Constellation ES",
        "capacity": "500GB",
        "rpm": "7200 RPM",
        "interface": "SATA",
        "transfer_rate": "3G",
        "cache": "32 MB",
        "form_factor": "3.5 in",
        "height": None,
    },
    # Source: Seagate Constellation.2 datasheet DS1719.4-1207 (ST9500620NS =
    # 500GB SATA 6Gb/s 7.2K, 64MB, 2.5", 15mm z-height, 512n). HPE markets it as
    # a 3G midline part; drive is natively 6Gb/s. HPE option 507750-B21 / spare
    # 508035-001 (HPE MM0500EBKAE). 15mm height — needs the 15mm warning.
    "ST9500620NS": {
        "brand": "Seagate",
        "family": "Constellation.2",
        "capacity": "500GB",
        "rpm": "7200 RPM",
        "interface": "SATA III",
        "transfer_rate": "6G",
        "cache": "64 MB",
        "form_factor": "2.5 in",
        "height": "15mm",
    },
}
