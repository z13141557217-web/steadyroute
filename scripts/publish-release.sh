#!/bin/sh
# Publish (or refresh) the GitHub Release for a tagged version.
#
#   ./scripts/publish-release.sh            # the version in VERSION
#   ./scripts/publish-release.sh 0.4.3      # any earlier tag, e.g. to backfill
#
# The package is rebuilt from the tag in a temporary worktree, so the attached zip always
# matches the tagged commit no matter what is checked out here. Notes come from
# docs/releases/v<VERSION>.md. Only the newest tag is marked "Latest".
set -eu

REPO='z13141557217-web/steadyroute'
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${1:-$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")}
TAG="v$VERSION"
NOTES="$PROJECT_DIR/docs/releases/$TAG.md"

if ! command -v gh >/dev/null 2>&1; then
  echo "需要 GitHub CLI：brew install gh，然后 gh auth login" >&2
  exit 1
fi
if ! git -C "$PROJECT_DIR" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "本地没有标签 $TAG；先 git fetch --tags，或确认版本号" >&2
  exit 1
fi
if [ ! -f "$NOTES" ]; then
  echo "缺少发布说明：$NOTES" >&2
  exit 1
fi

WORK=$(mktemp -d)
cleanup() {
  git -C "$PROJECT_DIR" worktree remove --force "$WORK/src" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

echo "从 $TAG 构建发布包（会运行全部检查，约 1 分钟）…"
git -C "$PROJECT_DIR" worktree add -q --detach "$WORK/src" "$TAG"
(cd "$WORK/src" && ./scripts/build-release.sh >"$WORK/build.log" 2>&1) || {
  tail -30 "$WORK/build.log" >&2
  echo "构建失败，未发布" >&2
  exit 1
}
ZIP="$WORK/src/dist/steadyroute-$VERSION.zip"
EXPECTED=$(git -C "$PROJECT_DIR" rev-list -n 1 "$TAG")
ACTUAL=$(unzip -p "$ZIP" "steadyroute-$VERSION/GIT_COMMIT" | tr -d '[:space:]')
if [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "发布包 commit $ACTUAL 与标签 $EXPECTED 不一致，未发布" >&2
  exit 1
fi

NEWEST=$(git -C "$PROJECT_DIR" tag -l 'v[0-9]*' --sort=-v:refname | head -n 1)
if [ "$NEWEST" = "$TAG" ]; then LATEST=--latest; else LATEST=--latest=false; fi

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release edit "$TAG" --repo "$REPO" --title "SteadyRoute $TAG" --notes-file "$NOTES" "$LATEST"
  gh release upload "$TAG" --repo "$REPO" --clobber "$ZIP" "$ZIP.sha256"
  echo "已更新 GitHub Release $TAG"
else
  gh release create "$TAG" --repo "$REPO" --verify-tag --title "SteadyRoute $TAG" \
    --notes-file "$NOTES" "$LATEST" "$ZIP" "$ZIP.sha256"
  echo "已发布 GitHub Release $TAG"
fi
