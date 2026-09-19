#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")

exec python3 "$PROJECT_DIR/scripts/deploy.py" deploy \
  --package "$PROJECT_DIR/dist/steadyroute-$VERSION.zip" \
  --checksum "$PROJECT_DIR/dist/steadyroute-$VERSION.zip.sha256" \
  "$@"
