#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 -m py_compile "$PROJECT_DIR/src/steadyroute/weighted_router.py"
python3 -m py_compile "$PROJECT_DIR/src/steadyroute/state_contract.py"
python3 -m py_compile "$PROJECT_DIR/src/steadyroute/route_policy.py"
python3 -m py_compile "$PROJECT_DIR/src/steadyroute/candidate_registry.py"
python3 -m py_compile "$PROJECT_DIR/src/steadyroute/health_model.py"
python3 -m py_compile "$PROJECT_DIR/src/steadyroute/runtime_metrics.py"
python3 -m py_compile "$PROJECT_DIR/src/steadyroute/logging_setup.py"
python3 -m py_compile "$PROJECT_DIR/src/steadyroute/clash_group_deploy.py"
python3 -m py_compile "$PROJECT_DIR/scripts/deploy.py"
python3 -m py_compile "$PROJECT_DIR/scripts/manage-clash-groups.py"
python3 -m py_compile "$PROJECT_DIR/scripts/verify-clash-discovery.py"
python3 "$PROJECT_DIR/scripts/generate-groups.py" --check
python3 -m unittest discover -s "$PROJECT_DIR/tests" -p 'test_*.py' -v

MIHOMO_CORE="/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"
if [ -x "$MIHOMO_CORE" ]; then
  python3 "$PROJECT_DIR/scripts/validate-mihomo-config.py" --core "$MIHOMO_CORE"
fi

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
