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

if [[ "$HITS" -ne 0 ]]; then
  echo "audit-no-product-data: FAIL — product/category identifiers found in the PUBLIC tree." >&2
  echo "The public repo must ship zero product/category specifics (see ebay-seller-tool#45)." >&2
  exit 1
fi
echo "audit-no-product-data: PASS — no product/category identifiers in the public tree."
exit 0
