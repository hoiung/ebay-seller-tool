#!/usr/bin/env bash
# Forward-prevention oracle (ebay-seller-tool#45 AC 5.1).
#
# The PUBLIC repo must ship ZERO product/category specifics. This is the CI guard
# that FAILS (exit 1) if a real product/category identifier re-enters the working
# tree. It is the SHAPE sweep — it catches new/unlisted identifiers by PATTERN.
#
# Token set embedded here (SHAPES + generic markers ONLY — this script must not
# itself leak our inventory, so it carries NO plaintext model or series literals;
# the exhaustive literal model/series list lives in the PRIVATE ebay-ops companion
# record, and the literal-identifier match is delegated to the hashed
# .secret-blocklist per AC 5.3):
#   (a) the generic eBay category id 56083 + the full category NAME phrase
#   (b) interface-key tokens (anchored: 'sata ii' must NOT match 'sata iii')
#   (c) OEM part-number SHAPE regexes (vendor-prefixed — the FORM, not a model)
#   (d) HPE option/spare part-number SHAPE
#
# Usage:  bash scripts/audit-no-product-data.sh      # exit 0 = clean, 1 = leak
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# Excluded from the sweep: VCS / venv / caches + this oracle itself + the
# blocklist/allowlist (which legitimately reference identifier patterns).
EXCLUDES=(
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules
  --exclude-dir=__pycache__ --exclude-dir=.ruff_cache --exclude-dir=.pytest_cache
  --exclude="audit-no-product-data.sh"
  --exclude=".secret-blocklist" --exclude=".secret-allowlist"
)

# (a) Generic eBay-taxonomy markers (NOT our inventory). Case-insensitive.
GENERIC_PATTERNS='(\b56083\b|Internal Hard Disk Drives)'
# (b) Interface-key tokens. Anchored so 'sata ii' does NOT match the generic
#     value 'sata iii' (which may appear in schema placeholders). Case-insensitive.
INTERFACE_PATTERNS='(12gb/s|sas-3|sata ii([^i]|$)|3gb/s)'
# (c) OEM part-number SHAPE regexes (vendor-prefixed). Case-SENSITIVE (OEM models
#     are upper-case) to avoid false-positives on lower-case prose.
OEM_SHAPE='(\bST[0-9]{3,4}[A-Z0-9]{4,8}\b|\bMB[0-9]{4}G[A-Z]{2,5}\b|\bEG[0-9]{4}[A-Z]{4,6}\b|\bEH[0-9]{4}[A-Z]{4,6}\b|\bMM[0-9]{4}[A-Z]{4,6}\b|\bHU[SC][0-9]{6,9}[A-Z]{2,3}[0-9]{3}\b|\bMG0[0-9]ACA[0-9]{3}N?\b|\bAL[0-9]{2}[A-Z]{2,3}[0-9]{3}N?\b|\bWD[0-9]{4}[A-Z]{3,5}\b)'
# (d) HPE option / spare part numbers (NNNNNN-B21 / NNNNNN-001). Case-sensitive.
HPE_PARTS='\b[0-9]{6}-(B21|001)\b'

# (e) Series names. Unlike (a)-(d) these have NO detectable SHAPE (they are
#     arbitrary product-line words), so they cannot be embedded here as literals
#     without re-leaking the seller's product range into this PUBLIC script
#     (issue #45 "taxonomy-private"). Instead they are sourced at RUNTIME from the
#     private taxonomy: when EBAY_LISTING_DATA_DIR is set (the operator's
#     data-aware env, or a CI with the private data provisioned), anchored series
#     patterns are derived from series-taxonomy.yaml and swept. When it is unset
#     (a bare public clone / vanilla public CI) the series class is SKIPPED with a
#     documented notice — the shape sweep still runs and series literals are also
#     recorded in the hashed .secret-blocklist. Bare common-colour series
#     (red/gold/purple) are anchored to a vendor prefix to avoid a false-positive
#     bomb (AC 5.1(d)).
SERIES_PATTERN=""
_taxo="${EBAY_LISTING_DATA_DIR:-}/series-taxonomy.yaml"
if [[ -n "${EBAY_LISTING_DATA_DIR:-}" && -f "$_taxo" ]]; then
  SERIES_PATTERN=$(python3 - "$_taxo" <<'PY'
import sys, re

try:
    import yaml

    data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    sys.exit(0)
tax = data.get("taxonomy", {}) or {}
# ONLY series_names (the product-IDENTITY list). preserved_phrases is a
# tokenisation aid that also carries GENERIC feature tokens ("hot swap",
# "self encrypting") which legitimately appear in category-agnostic public code
# (feature detection) — sourcing them would false-positive on generic machinery.
names = list((tax.get("comp_filter", {}) or {}).get("series_names", []) or [])
# Generic colour words — anchor to a vendor prefix so bare "red"/"gold" in prose
# does not false-positive (AC 5.1(d)); the full OEM model shapes cover the drives.
COLOUR = {"red", "gold", "purple"}
pats, seen = [], set()
for raw in names:
    s = str(raw).strip().lower()
    if not s or s in seen:
        continue
    seen.add(s)
    toks = s.split()
    if toks and toks[0] in COLOUR:
        pats.append(r"\bwd\s+" + r"\s+".join(re.escape(t) for t in toks) + r"\b")
    else:
        pats.append(r"\b" + r"\s+".join(re.escape(t) for t in toks) + r"\b")
print("|".join(pats))
PY
)
fi

HITS=0
# scan <label> <case-insensitive: i|""> <pattern>
scan() {
  local label="$1" ci="$2" pat="$3" out
  if [[ "$ci" == "i" ]]; then
    out=$(grep -rinE "${EXCLUDES[@]}" "$pat" . 2>/dev/null || true)
  else
    out=$(grep -rnE "${EXCLUDES[@]}" "$pat" . 2>/dev/null || true)
  fi
  if [[ -n "$out" ]]; then
    echo "audit-no-product-data: MATCH [$label]"
    echo "$out"
    echo ""
    HITS=1
  fi
}

scan "category-id+name" "i" "$GENERIC_PATTERNS"
scan "interface-tokens" "i" "$INTERFACE_PATTERNS"
scan "oem-part-shape" "" "$OEM_SHAPE"
scan "hpe-part-shape" "" "$HPE_PARTS"
if [[ -n "$SERIES_PATTERN" ]]; then
  scan "series-name" "i" "($SERIES_PATTERN)"
else
  echo "audit-no-product-data: NOTE — series-name sweep SKIPPED (no series patterns" \
       "available: EBAY_LISTING_DATA_DIR unset, taxonomy absent, or series_names empty);" \
       "shape sweep only. Series literals are recorded in the hashed .secret-blocklist; set" \
       "EBAY_LISTING_DATA_DIR to enable the runtime-sourced series sweep."
fi

if [[ "$HITS" -ne 0 ]]; then
  echo "audit-no-product-data: FAIL — product/category identifiers found in the PUBLIC tree." >&2
  echo "The public repo must ship zero product/category specifics (see ebay-seller-tool#45)." >&2
  exit 1
fi
echo "audit-no-product-data: PASS — no product/category identifiers in the public tree."
exit 0
