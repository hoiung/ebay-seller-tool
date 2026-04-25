"""End-to-end workflow chain test for the Pricing Review (#13 Phase 7 AP #18).

The skill-orchestration workflow chains 8+ helpers:
  fetch_competitor_prices → filter_clean_competitors → drop_stale_competitors
  → percentile computation → compute_under_pricing / compute_over_pricing
  → compute_content_benchmarks → tokenise_title / compute_keyword_diff
  → append_snapshot

Each helper is unit-tested. THIS test verifies they WIRE together correctly
end-to-end with a realistic Browse-API response shape — answering the
AP #18 question 'does the full pipeline produce sensible output when
the parts are connected the way the skill orchestrator will connect them?'

Live 22-listing sweep is invoked at `/ebay-seller-tool` time (skill-
orchestration level — not from a Python test) per Issue #13 Phase 7.1.
This test is the workflow plumbing gate that closes Phase 7 from the
implementation side.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ebay import browse, oauth
from ebay.analytics import compute_over_pricing, compute_under_pricing
from ebay.content_benchmark import compute_content_benchmarks
from ebay.snapshots import append_snapshot
from ebay.title_benchmark import compute_keyword_diff


def setup_function() -> None:
    oauth.reset_token_cache()


def _run(coro):
    return asyncio.run(coro)


def _fake_browse_client(payload: dict) -> MagicMock:
    client = MagicMock()
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    resp.text = "{}"
    resp.json.return_value = payload
    client.get.return_value = resp
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def _build_comp_summary(items: list[dict]) -> dict:
    return {"itemSummaries": items}


def _comp_payload(
    *,
    item_id: str,
    title: str,
    price: float,
    age_days: int = 30,
    condition: str = "Used",
    additional_images: int = 4,
    top_rated: bool = True,
    returns_within: int = 30,
    best_offer: bool = False,
) -> dict:
    creation = (datetime.now(timezone.utc) - timedelta(days=age_days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    return {
        "itemId": item_id,
        "title": title,
        "price": {"value": str(price), "currency": "GBP"},
        "seller": {
            "username": f"comp-{item_id}",
            "feedbackPercentage": "99.5",
            "feedbackScore": 1000,
        },
        "condition": condition,
        # Issue #14 Phase 2.1 — numeric conditionId for equivalence-class scoring.
        "conditionId": "3000",
        "itemWebUrl": f"https://ebay.co.uk/itm/{item_id}",
        "itemCreationDate": creation,
        "image": {"imageUrl": f"https://i.ebayimg.com/{item_id}.jpg"},
        "additionalImages": [
            {"imageUrl": f"https://i.ebayimg.com/{item_id}_{n}.jpg"}
            for n in range(additional_images)
        ],
        "topRatedBuyingExperience": top_rated,
        "returnTerms": {"returnsAccepted": True, "returnsWithinDays": returns_within},
        "bestOfferEnabled": best_offer,
    }


def _percentiles(prices: list[float]) -> dict[str, float]:
    """Helper — replicate browse.py's percentile arithmetic for the chain test."""
    sorted_p = sorted(prices)
    count = len(sorted_p)
    return {
        "p25": sorted_p[max(0, count // 4)],
        "p40": sorted_p[max(0, int(count * 0.40))],
        "p55": sorted_p[max(0, int(count * 0.55))],
        "p65": sorted_p[max(0, int(count * 0.65))],
        "p75": sorted_p[min(count - 1, (3 * count) // 4)],
    }


def test_full_workflow_chain_underpriced_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whole-chain plumbing test: underpriced own listing flows through all 8 helpers.

    Setup:
      - Own listing: ST2000NX0253, £20.00, 2 photos, no Best Offer, 14d return
      - 6 clean comps in £28-£40 range, all top-rated, all 30d returns
      - Own price below p25 → A signal True

    Asserts:
      - Browse → 6 listings parsed with all extension fields
      - Apple-to-apples filter retains all 6 (perfect MPN match)
      - Stale dropper drops 1 oldest
      - Under-pricing detector triggers on signal A only → ok (1/3)
      - Content benchmarks: photo + best_offer + top_rated + returns ALL flagged
      - Title diff surfaces missing high-freq tokens
      - append_snapshot writes analysis_baseline event
    """
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    snap_path = tmp_path / "snap.jsonl"
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(snap_path))

    # 1. Browse API response — 6 clean comps for ST2000NX0253.
    comp_items = [
        _comp_payload(
            item_id=f"v1|comp{i}",
            title=f"ST2000NX0253 2.5 SAS Enterprise HDD {i}TB Server",
            price=p,
            age_days=ages[i],
            top_rated=True,
            additional_images=5,
            returns_within=30,
            best_offer=(i >= 2),  # 4 of 6 True (67%) — flagged after stale-drop
        )
        for i, (p, ages) in enumerate(
            zip([28.0, 30.0, 32.0, 35.0, 38.0, 40.0], [[15, 30, 45, 60, 90, 365]] * 6)
        )
    ]
    fake = _fake_browse_client(_build_comp_summary(comp_items))

    own_listing = {
        "item_id": "999",
        "title": "ST2000NX0253 2.5 SAS HDD",
        "price": "20.00",
        "specifics": {
            "MPN": ["ST2000NX0253"],
            "Form Factor": ['2.5"'],
        },
        "condition_id": "3000",
        "condition_name": "Used",
        "photos": ["a.jpg", "b.jpg"],  # only 2 photos
        "best_offer_enabled": False,
        "return_policy": {"period_days": 14, "returns_accepted": True},
        "watch_count": 5,
        "quantity_sold": 0,
        "days_on_site": 30,
    }

    # 2. Browse + filter chain.
    with patch("ebay.browse.get_browse_session", return_value=fake):
        comps = _run(browse.fetch_competitor_prices(part_number="ST2000NX0253"))

    assert comps["count"] == 6
    # Verify Phase 1 extensions present.
    assert all("item_creation_date" in c for c in comps["listings"])
    assert all("returns_within_days" in c for c in comps["listings"])

    # Apple-to-apples filter (Phase 2.1) — all 6 should pass since titles
    # contain MPN, are not bundles, match form factor, condition, recent.
    clean = browse.filter_clean_competitors(own_listing, comps["listings"], threshold=0.6)
    assert len(clean) == 6

    # Stale dropper at 10% of N=6 → drop 0 (floor). Test with explicit 20% to
    # actually drop 1.
    fresh = browse.drop_stale_competitors(clean, drop_pct=20.0)
    assert len(fresh) == 5  # dropped the 365d one

    # 3. Compute percentiles from clean comp prices.
    fresh_prices = [c["price"] for c in fresh]
    pcts = _percentiles(fresh_prices)
    # With prices [28, 30, 32, 35, 38] (oldest 40.0 dropped), p25 = sorted[1] = 30
    assert pcts["p25"] == 30.0

    # 4. Under-pricing detector — own £20 < p25 £30 → signal A True.
    under = compute_under_pricing(
        live_price=20.0,
        p25_clean=pcts["p25"],
        units_sold_per_day=0.0,  # B: false
        days_to_sell_median=30,  # C: false
        p40_clean=pcts["p40"],
        p55_clean=pcts["p55"],
    )
    assert under["signals"]["A"] is True
    assert under["signals"]["B"] is False
    assert under["signals"]["C"] is False
    assert under["verdict"] == "ok"  # 1/3 → ok

    # 5. Over-pricing detector — own £20 NOT > p75 £38 → ok.
    over = compute_over_pricing(
        live_price=20.0,
        p75_clean=pcts["p75"],
        watchers=5,
        units_sold=0,
        days_on_site=30,
    )
    assert over["verdict"] == "ok"

    # 6. Content benchmarks — own has 2 photos vs comp p25 (with primary +
    # 5 additional = 6 each), no Best Offer vs 50% comps, not top-rated vs
    # 100% comps, 14d returns vs 30d p50.
    content = compute_content_benchmarks(own_listing, fresh, own_top_rated=False)
    assert content["photo_count"]["verdict"] == "flagged"
    # NOTE: comp_p25 of [6,6,6,6,6] = 6.0, own=2 → flagged.
    # best_offer_posture: 3 of 5 enabled (60%, after dropping oldest) AND own=False → flagged.
    assert content["best_offer_posture"]["verdict"] == "flagged"
    assert content["top_rated_seller_gap"]["verdict"] == "flagged"  # 100% > 40%
    assert content["returns_policy_generosity"]["verdict"] == "flagged"  # 14 < 30

    # 7. Title benchmarking — own missing 'enterprise' / 'server'.
    diff = compute_keyword_diff(
        own_listing["title"],
        [c["title"] for c in fresh],
        mandatory_keywords=["Seagate", "ST2000NX0253", "2TB", "SAS", "HDD"],
        frequency_threshold_pct=50.0,
    )
    candidate_tokens = {c["token"] for c in diff["candidates"]}
    assert "enterprise" in candidate_tokens
    assert "server" in candidate_tokens
    # mandatory anchors not recommended.
    assert "st2000nx0253" not in candidate_tokens

    # 8. Snapshot — emit analysis_baseline event.
    append_snapshot(
        "analysis_baseline",
        own_listing["item_id"],
        {
            "price_gbp": float(own_listing["price"]),
            "watch_count": own_listing["watch_count"],
            "verdicts": {
                "under": under["verdict"],
                "over": over["verdict"],
                "content_flags": sum(1 for v in content.values() if v["verdict"] == "flagged"),
                "title_candidates": len(diff["candidates"]),
            },
            "source": "workflow_chain_test",
        },
    )

    # 9. Verify snapshot persisted with full chain context.
    assert snap_path.exists()
    row = json.loads(snap_path.read_text().strip())
    assert row["event"] == "analysis_baseline"
    assert row["item_id"] == "999"
    assert row["verdicts"]["under"] == "ok"
    assert row["verdicts"]["content_flags"] == 4  # all 4 benchmarks flagged
    assert row["verdicts"]["title_candidates"] >= 1


def test_full_workflow_chain_underpriced_red_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under-pricing RED triggers when price < p25 + velocity > 0.1 + days < 7."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(tmp_path / "snap.jsonl"))

    comp_items = [
        _comp_payload(
            item_id=f"v1|c{i}",
            title=f"ST2000NX0253 2.5 SAS Enterprise HDD {i}",
            price=p,
        )
        for i, p in enumerate([35.0, 40.0, 45.0, 50.0])
    ]
    fake = _fake_browse_client(_build_comp_summary(comp_items))
    own_listing = {
        "item_id": "777",
        "title": "ST2000NX0253 SAS HDD",
        "specifics": {"MPN": ["ST2000NX0253"], "Form Factor": ['2.5"']},
        "condition_id": "3000",
        "condition_name": "Used",
    }

    with patch("ebay.browse.get_browse_session", return_value=fake):
        comps = _run(browse.fetch_competitor_prices(part_number="ST2000NX0253"))

    clean = browse.filter_clean_competitors(own_listing, comps["listings"], threshold=0.6)
    pcts = _percentiles([c["price"] for c in clean])

    under = compute_under_pricing(
        live_price=20.0,  # < p25 35 (A True)
        p25_clean=pcts["p25"],
        units_sold_per_day=0.5,  # > 0.1 (B True)
        days_to_sell_median=3,  # < 7 (C True)
        p40_clean=pcts["p40"],
        p55_clean=pcts["p55"],
    )
    assert under["verdict"] == "RED"
    assert under["recommended_floor"] == pcts["p40"]
    assert under["recommended_ceiling"] == pcts["p55"]


def test_full_workflow_chain_overpriced_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Overpricing case: live_price > p75, watchers > 0, no sales, days > 21."""
    monkeypatch.delenv("EBAY_OWN_SELLER_USERNAME", raising=False)
    monkeypatch.setenv("EBAY_SNAPSHOT_PATH", str(tmp_path / "snap.jsonl"))

    comp_items = [
        _comp_payload(
            item_id=f"v1|c{i}",
            title=f"ST2000NX0253 2.5 SAS Enterprise HDD {i}",
            price=p,
        )
        for i, p in enumerate([28.0, 30.0, 32.0, 35.0, 38.0])
    ]
    fake = _fake_browse_client(_build_comp_summary(comp_items))
    own_listing = {
        "item_id": "555",
        "title": "ST2000NX0253 SAS HDD",
        "specifics": {"MPN": ["ST2000NX0253"], "Form Factor": ['2.5"']},
        "condition_id": "3000",
        "condition_name": "Used",
    }

    with patch("ebay.browse.get_browse_session", return_value=fake):
        comps = _run(browse.fetch_competitor_prices(part_number="ST2000NX0253"))

    clean = browse.filter_clean_competitors(own_listing, comps["listings"], threshold=0.6)
    pcts = _percentiles([c["price"] for c in clean])

    over = compute_over_pricing(
        live_price=50.0,  # > p75 (A True)
        p75_clean=pcts["p75"],
        watchers=3,  # > 0 (B True)
        units_sold=0,  # == 0 (C True)
        days_on_site=44,  # > 21 (D True)
        p55_clean=pcts["p55"],
        p65_clean=pcts["p65"],
    )
    assert over["verdict"] == "OVERPRICED"
    assert over["recommended_floor"] == pcts["p55"]
    assert over["recommended_ceiling"] == pcts["p65"]
