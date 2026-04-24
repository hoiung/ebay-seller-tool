"""
Title keyword benchmarking (Issue #13 Phase 3).

`tokenise_title` lowercases + ASCII-folds + strips filler phrases + preserves
known multi-word collocations, then splits on non-alphanumeric.

`compute_keyword_diff` compares own title against a set of clean (apple-to-
apples filtered) competitor titles, surfaces tokens that appear in
≥`frequency_threshold_pct`% of comps but NOT in own title, ranked by
`(frequency × min(remaining_char_budget, char_cost))` per round-2 F-B.

Mandatory anchor tokens (Brand, Capacity, etc.) are excluded from the
recommendation list — they are already enforced by SKILL.md "Title Rules"
canonical, so suggesting them adds noise.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing_and_content.yaml"
_TITLE_CHAR_LIMIT = 80  # eBay title character cap.

_cached_config: dict[str, Any] | None = None


def _load_pricing_and_content_config() -> dict[str, Any]:
    """Load + cache the title knobs YAML. Re-reads if file missing on first call."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config/pricing_and_content.yaml not found at {_CONFIG_PATH}; "
            "title benchmarking requires it for filler_words + preserved_phrases."
        )
    with open(_CONFIG_PATH) as f:
        _cached_config = yaml.safe_load(f) or {}
    return _cached_config


def _reset_cache_for_tests() -> None:
    """Test hook to force re-load (e.g. when tests stub config path)."""
    global _cached_config
    _cached_config = None


def _ascii_fold(s: str) -> str:
    """Strip diacritics: 'Café' → 'cafe'."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def tokenise_title(
    title: str,
    *,
    filler_words: list[str] | None = None,
    preserved_phrases: list[str] | None = None,
) -> list[str]:
    """Tokenise a listing title for keyword analysis.

    Steps:
      1. Lowercase + ASCII-fold (diacritics → base).
      2. Replace preserved phrases ("enterprise capacity") with single
         underscore-joined tokens before splitting.
      3. Strip filler phrases (word-bounded regex match).
      4. Split on non-alphanumeric (alphanumeric + underscore retained).
      5. Drop empty tokens and order-preserve.

    `filler_words` and `preserved_phrases` default to values from
    config/pricing_and_content.yaml `title:` section.
    """
    if filler_words is None or preserved_phrases is None:
        cfg = _load_pricing_and_content_config().get("title") or {}
        if filler_words is None:
            filler_words = cfg.get("filler_words", []) or []
        if preserved_phrases is None:
            preserved_phrases = cfg.get("preserved_phrases", []) or []

    # 1. lower + ASCII fold
    s = _ascii_fold(title.lower())

    # 2. preserved phrases — replace with underscore-joined token to survive
    #    the splitter intact. Sort by length DESC so longer phrases win first
    #    ("iron wolf pro" before "iron wolf").
    for phrase in sorted(preserved_phrases, key=len, reverse=True):
        phrase_lc = phrase.lower()
        if phrase_lc in s:
            s = s.replace(phrase_lc, phrase_lc.replace(" ", "_"))

    # 3. filler words — word-bounded regex strip. Sort by length DESC.
    for filler in sorted(filler_words, key=len, reverse=True):
        s = re.sub(rf"(?<!\w){re.escape(filler.lower())}(?!\w)", " ", s)

    # 4. split on non-alphanumeric (keep underscores for collocations).
    tokens = re.findall(r"[a-z0-9_]+", s)

    # 5. drop empty.
    return [t for t in tokens if t]


def compute_keyword_diff(
    own_title: str,
    clean_comp_titles: list[str],
    *,
    frequency_threshold_pct: float = 50.0,
    mandatory_keywords: list[str] | None = None,
    title_char_limit: int = _TITLE_CHAR_LIMIT,
) -> dict[str, Any]:
    """Diff own title vs clean comp titles, surface missing high-freq keywords.

    Algorithm:
      1. Tokenise all titles.
      2. Compute per-token frequency across DISTINCT comp titles (token only
         counts once per comp, not per occurrence — prevents repetition bias).
      3. Filter to tokens at >= frequency_threshold_pct% of comps.
      4. Drop tokens already present in own_title (no-op recommendation).
      5. Drop mandatory anchor tokens (already enforced by SKILL.md).
      6. Rank survivors by (freq_pct × min(budget_remaining, char_cost)).

    Args:
        own_title: own listing title.
        clean_comp_titles: list of comp titles AFTER apple-to-apples filtering.
        frequency_threshold_pct: minimum %% of comps in which a token must
            appear to be a candidate. Default 50%.
        mandatory_keywords: literal tokens already enforced by SKILL.md
            (orchestrator resolves these from drive-class category list +
            listing specifics). Case-insensitive match.
        title_char_limit: eBay title char cap (default 80).

    Returns:
        {
            "candidates": [{token, freq_pct, char_cost, rank_score}, ...],
            "own_char_count": int,
            "budget_remaining": int,
            "comps_analysed": int,
        }
    """
    if mandatory_keywords is None:
        mandatory_keywords = []

    own_tokens = set(tokenise_title(own_title))
    own_char_count = len(own_title)
    budget_remaining = max(0, title_char_limit - own_char_count)

    n_comps = len(clean_comp_titles)
    if n_comps == 0:
        return {
            "candidates": [],
            "own_char_count": own_char_count,
            "budget_remaining": budget_remaining,
            "comps_analysed": 0,
        }

    # Frequency analysis: each comp contributes ONE +1 per distinct token.
    freq: dict[str, int] = {}
    for ct in clean_comp_titles:
        for token in set(tokenise_title(ct)):
            freq[token] = freq.get(token, 0) + 1

    threshold_count = (frequency_threshold_pct / 100.0) * n_comps
    mandatory_lower = {str(m).lower() for m in mandatory_keywords}

    candidates: list[dict[str, Any]] = []
    for token, count in freq.items():
        if count < threshold_count:
            continue
        if token in own_tokens:
            continue
        if token in mandatory_lower:
            continue
        freq_pct = round(100.0 * count / n_comps, 1)
        char_cost = len(token) + 1  # +1 for the separating space.
        rank_score = round(freq_pct * min(budget_remaining, char_cost), 2)
        candidates.append(
            {
                "token": token,
                "freq_pct": freq_pct,
                "char_cost": char_cost,
                "rank_score": rank_score,
            }
        )

    candidates.sort(key=lambda x: x["rank_score"], reverse=True)

    return {
        "candidates": candidates,
        "own_char_count": own_char_count,
        "budget_remaining": budget_remaining,
        "comps_analysed": n_comps,
    }
