#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")
DIST_DIR="$PROJECT_DIR/dist"
STAGE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/steadyroute-release.XXXXXX")
PACKAGE_DIR="$STAGE_DIR/steadyroute-$VERSION"

cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT INT TERM

"$PROJECT_DIR/scripts/check.sh"
mkdir -p "$DIST_DIR" "$PACKAGE_DIR/src" "$PACKAGE_DIR/config" "$PACKAGE_DIR/deploy" "$PACKAGE_DIR/docs"

cp "$PROJECT_DIR/src/steadyroute/weighted_router.py" "$PACKAGE_DIR/src/"
cp "$PROJECT_DIR/src/steadyroute/dashboard.html" "$PACKAGE_DIR/src/"
cp "$PROJECT_DIR/config/clash-verge/groups.yaml" "$PACKAGE_DIR/config/"
cp "$PROJECT_DIR/deploy/macos/com.nurture.clash-stability-router.plist" "$PACKAGE_DIR/deploy/"
cp "$PROJECT_DIR/VERSION" "$PROJECT_DIR/CHANGELOG.md" "$PACKAGE_DIR/"
cp "$PROJECT_DIR/docs/RELEASE.md" "$PROJECT_DIR/docs/OPERATIONS.md" "$PACKAGE_DIR/docs/"

if git -C "$PROJECT_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  git -C "$PROJECT_DIR" rev-parse HEAD > "$PACKAGE_DIR/GIT_COMMIT"
else
  printf '%s\n' 'uncommitted-baseline' > "$PACKAGE_DIR/GIT_COMMIT"
fi

COMMIT=$(tr -d '[:space:]' < "$PACKAGE_DIR/GIT_COMMIT")
BUILT_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
printf '{\n  "schema_version": 1,\n  "version": "%s",\n  "commit": "%s",\n  "built_at": "%s"\n}\n' \
  "$VERSION" "$COMMIT" "$BUILT_AT" > "$PACKAGE_DIR/RELEASE.json"

(
  cd "$PACKAGE_DIR"
  find . -type f ! -name MANIFEST.sha256 -print | LC_ALL=C sort | while IFS= read -r file; do
    digest=$(shasum -a 256 "$file" | awk '{print $1}')
    printf '%s  %s\n' "$digest" "${file#./}"
  done > MANIFEST.sha256
)

(cd "$STAGE_DIR" && /usr/bin/zip -qr "$DIST_DIR/steadyroute-$VERSION.zip" "steadyroute-$VERSION")
PACKAGE_SHA=$(shasum -a 256 "$DIST_DIR/steadyroute-$VERSION.zip" | awk '{print $1}')
printf '%s  %s\n' "$PACKAGE_SHA" "steadyroute-$VERSION.zip" > "$DIST_DIR/steadyroute-$VERSION.zip.sha256"

echo "$DIST_DIR/steadyroute-$VERSION.zip"
