#!/bin/sh
# Build the Mihomo core the integration tests run against (tests/test_real_core.py).
#
#   ./scripts/build-core.sh /path/to/mihomo
#   STEADYROUTE_MIHOMO=/path/to/mihomo ./scripts/check.sh
#
# Development and CI only; nothing here is shipped or used by the installed product.
# The source is pinned by commit, so what is built is exactly the tagged release. Needs git and Go.
set -eu

VERSION=v1.19.32
COMMIT=88dcbf7f1614a67c3b36b848ee3592dfa92ada36
OUT=${1:?usage: build-core.sh OUTPUT_FILE}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM
git clone -q --depth 1 --branch "$VERSION" https://github.com/MetaCubeX/mihomo "$WORK/src" 2>/dev/null
ACTUAL=$(git -C "$WORK/src" rev-parse HEAD)
if [ "$ACTUAL" != "$COMMIT" ]; then
  echo "mihomo $VERSION is at $ACTUAL, expected $COMMIT; not building" >&2
  exit 1
fi
(cd "$WORK/src" && CGO_ENABLED=0 go build -tags with_gvisor -trimpath \
  -ldflags "-X github.com/metacubex/mihomo/constant.Version=$VERSION -w -s" -o "$WORK/mihomo" .)
mkdir -p "$(dirname -- "$OUT")"
mv "$WORK/mihomo" "$OUT"
"$OUT" -v
