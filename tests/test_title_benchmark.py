"""Unit tests for ebay.title_benchmark (Issue #13 Phase 3)."""

from __future__ import annotations

import pytest

from ebay.title_benchmark import (
    _reset_cache_for_tests,
    compute_keyword_diff,
    tokenise_title,
)


def setup_function() -> None:
    _reset_cache_for_tests()


# === tokenise_title ============================================================


def test_tokenise_lowercase_basic() -> None:
    tokens = tokenise_title("Seagate ST2000NX0253 2TB SAS HDD")
    assert "seagate" in tokens
    assert "st2000nx0253" in tokens
    assert "2tb" in tokens
    assert "sas" in tokens
    assert "hdd" in tokens


def test_tokenise_strips_filler_words() -> None:
    """Phase 3.1.1 — filler list strip."""
    tokens = tokenise_title("Seagate 2TB SAS UK Stock Fast Shipping Excellent Condition")
    # filler 'uk stock', 'fast shipping', 'excellent', 'condition' should be gone.
    assert "uk" not in tokens
    assert "stock" not in tokens
    assert "shipping" not in tokens
    assert "fast" not in tokens
    assert "excellent" not in tokens
    assert "condition" not in tokens
    # core terms survive.
    assert "seagate" in tokens
    assert "2tb" in tokens


def test_tokenise_preserves_collocations() -> None:
    """Phase 3.1.1 — preserved_phrases keeps 'enterprise capacity' joined."""
    tokens = tokenise_title("Seagate Enterprise Capacity 2TB ST2000NX0253")
    # preserved_phrases joins as 'enterprise_capacity'.
    assert "enterprise_capacity" in tokens
    assert "enterprise" not in tokens
    assert "capacity" not in tokens


def test_tokenise_ascii_fold() -> None:
    """Diacritics → base ASCII."""
    tokens = tokenise_title("Café résumé naïve")
    assert "cafe" in tokens
    assert "resume" in tokens
    assert "naive" in tokens


def test_tokenise_punctuation_split() -> None:
    """Punctuation acts as splitter."""
    tokens = tokenise_title("Seagate-2TB,SAS;HDD")
    assert "seagate" in tokens
    assert "2tb" in tokens
    assert "sas" in tokens
    assert "hdd" in tokens


def test_tokenise_empty_returns_empty_list() -> None:
    assert tokenise_title("") == []


def test_tokenise_passes_explicit_filler_overrides_config() -> None:
    """Explicit filler_words override config — used by tests + orchestrator overrides."""
    tokens = tokenise_title("foo bar baz", filler_words=["bar"], preserved_phrases=[])
    assert "foo" in tokens
    assert "baz" in tokens
    assert "bar" not in tokens


# === compute_keyword_diff =====================================================


def test_keyword_diff_surfaces_missing_high_freq() -> None:
    """Token in 100% of comps but missing from own → top candidate."""
    own = "Seagate 2TB SAS HDD"
    comps = [
        "Seagate 2TB SAS Enterprise HDD",
        "Seagate 2TB SAS Enterprise NAS HDD",
        "Seagate 2TB SAS Enterprise Server HDD",
    ]
    result = compute_keyword_diff(own, comps, frequency_threshold_pct=50.0)
    candidate_tokens = [c["token"] for c in result["candidates"]]
    # "enterprise" appears in 100% of comps, missing from own → must be in candidates.
    assert "enterprise" in candidate_tokens


def test_keyword_diff_drops_tokens_already_in_own() -> None:
    """Tokens already in own title are NOT recommended."""
    own = "Seagate 2TB SAS Enterprise HDD"
    comps = ["Seagate 2TB SAS Enterprise HDD"] * 3
    result = compute_keyword_diff(own, comps)
    candidate_tokens = [c["token"] for c in result["candidates"]]
    assert "seagate" not in candidate_tokens
    assert "enterprise" not in candidate_tokens


def test_keyword_diff_drops_mandatory_keywords() -> None:
    """Phase 3.1.2 — mandatory_keywords excluded even at high freq."""
    own = "Cheap Drive Stuff"
    comps = ["Seagate 2TB SAS Enterprise HDD"] * 5
    result = compute_keyword_diff(
        own,
        comps,
        mandatory_keywords=["seagate"],  # mandatory anchor
        frequency_threshold_pct=50.0,
    )
    candidate_tokens = [c["token"] for c in result["candidates"]]
    assert "seagate" not in candidate_tokens
    # other 100%-freq tokens still surface.
    assert "enterprise" in candidate_tokens or "sas" in candidate_tokens


def test_keyword_diff_threshold_filters() -> None:
    """Tokens below threshold are dropped."""
    own = "x"
    comps = [
        "Seagate alpha",
        "Seagate beta",
        "Seagate gamma",
        "Seagate delta",
        "Seagate epsilon",
    ]
    result = compute_keyword_diff(own, comps, frequency_threshold_pct=80.0)
    candidate_tokens = [c["token"] for c in result["candidates"]]
    # 'seagate' = 100%, kept. alpha/beta/etc. = 20% each, dropped.
    assert "seagate" in candidate_tokens
    assert "alpha" not in candidate_tokens
    assert "beta" not in candidate_tokens


def test_keyword_diff_ranks_by_score() -> None:
    """Highest rank_score sorted first."""
    own = "x"
    comps = [
        "alpha very long token here that is large",
        "alpha very long token here that is large",
    ]
    result = compute_keyword_diff(own, comps, frequency_threshold_pct=50.0)
    if len(result["candidates"]) >= 2:
        for i in range(len(result["candidates"]) - 1):
            assert (
                result["candidates"][i]["rank_score"]
                >= result["candidates"][i + 1]["rank_score"]
            )


def test_keyword_diff_empty_comps_returns_empty_candidates() -> None:
    result = compute_keyword_diff("Seagate 2TB", [])
    assert result["candidates"] == []
    assert result["comps_analysed"] == 0


def test_keyword_diff_budget_remaining_clamped() -> None:
    """Own title at 80 chars → budget_remaining=0 → rank_score=0 for all."""
    own = "x" * 80
    comps = ["alpha beta gamma"] * 3
    result = compute_keyword_diff(own, comps, frequency_threshold_pct=50.0)
    assert result["budget_remaining"] == 0
    for c in result["candidates"]:
        assert c["rank_score"] == 0


def test_keyword_diff_worked_example() -> None:
    """Phase 3.1.4 — worked example matching round-2 F-B intent.

    Own listing missing 'enterprise' which appears in 4 of 5 comp titles (80%).
    Expected: enterprise surfaces as top candidate, mandatory anchors excluded.
    """
    own = "Seagate ST2000NX0253 2TB SAS HDD 2.5"
    comps = [
        "Seagate ST2000NX0253 2TB SAS Enterprise 2.5 HDD",
        "Seagate ST2000NX0253 2TB SAS Enterprise 2.5 HDD",
        "Seagate ST2000NX0253 2TB SAS Enterprise 2.5 HDD",
        "Seagate ST2000NX0253 2TB SAS Enterprise NAS 2.5 HDD",
        "Seagate ST2000NX0253 2TB SAS 2.5 HDD",  # no 'enterprise' here
    ]
    result = compute_keyword_diff(
        own,
        comps,
        mandatory_keywords=["Seagate", "ST2000NX0253", "2TB", "SAS", "HDD"],
        frequency_threshold_pct=50.0,
    )
    candidate_tokens = [c["token"] for c in result["candidates"]]
    assert "enterprise" in candidate_tokens
    # mandatory anchors excluded.
    assert "seagate" not in candidate_tokens
    assert "st2000nx0253" not in candidate_tokens
    assert "2tb" not in candidate_tokens
