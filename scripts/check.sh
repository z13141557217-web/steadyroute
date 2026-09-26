#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Byte-compile every Python file we ship or run (a hand-written list kept missing scripts).
python3 -m compileall -q "$PROJECT_DIR/src" "$PROJECT_DIR/scripts" "$PROJECT_DIR/sim" "$PROJECT_DIR/tests"
python3 "$PROJECT_DIR/scripts/generate-groups.py" --check
python3 -m unittest discover -s "$PROJECT_DIR/tests" -p 'test_*.py' -v

MIHOMO_CORE="/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"
if [ -x "$MIHOMO_CORE" ]; then
  python3 "$PROJECT_DIR/scripts/validate-mihomo-config.py" --core "$MIHOMO_CORE"
fi

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PROJECT_DIR/deploy/macos/com.nurture.clash-stability-router.plist"
fi

# Standard library only, so a missing tool can never turn this into a silent pass.
python3 "$PROJECT_DIR/scripts/leak-scan.py" "$PROJECT_DIR"

echo "SteadyRoute checks passed."
