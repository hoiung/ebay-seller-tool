#!/usr/bin/env bash
# Pre-commit hook: detect hardcoded credentials in source files
set -euo pipefail

ISSUES=$(grep -rnE "(password|secret|api_key|apikey|auth_token)\s*=\s*['\"][^'\"]+['\"]" \
  --include="*.py" --include="*.toml" --include="*.yml" --include="*.yaml" \
  --exclude-dir=.git --exclude-dir=.venv . \
  | grep -vE "(\.env\.example|test_|_test\.py|\.pre-commit)" || true)

if [ -n "$ISSUES" ]; then
  echo "ERROR - Possible hardcoded credentials found:"
  echo "$ISSUES"
  exit 1
fi
