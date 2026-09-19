#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 -m py_compile "$PROJECT_DIR/src/steadyroute/weighted_router.py"
python3 -m py_compile "$PROJECT_DIR/scripts/deploy.py"
python3 -m unittest discover -s "$PROJECT_DIR/tests" -p 'test_*.py' -v

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PROJECT_DIR/deploy/macos/com.nurture.clash-stability-router.plist"
fi

if (cd "$PROJECT_DIR" && rg -n --hidden --glob '!.git/**' --glob '!*.md' --glob '!CHANGELOG.md' --glob '!scripts/check.sh' \
  '(subscription-url|token[[:space:]]*[:=][[:space:]]*[^[:space:]]+|password[[:space:]]*[:=][[:space:]]*[^[:space:]]+)' \
  . >/dev/null 2>&1); then
  echo "Potential secret material detected; review before commit." >&2
  exit 1
fi

echo "SteadyRoute checks passed."
