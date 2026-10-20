#!/bin/sh
# Publish (or refresh) the GitHub Release for a tagged version.
#
#   ./scripts/publish-release.sh            # the version in VERSION
#   ./scripts/publish-release.sh 0.4.3      # any earlier tag, e.g. to backfill
#   ./scripts/publish-release.sh --missing  # every tag that has no Release yet (also run by CI)
#
# The package is rebuilt from the tag in a temporary worktree, so the attached zip always
# matches the tagged commit no matter what is checked out here. Notes come from
# docs/releases/v<VERSION>.md. Only the newest tag is marked "Latest".
set -eu

REPO=${GITHUB_REPOSITORY:-z13141557217-web/steadyroute}
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ "${1:-}" = "--missing" ]; then
  # A tag without a Release page is what made GitHub show v0.4.6 as the newest version
  # while v0.5.0 to v0.5.4 were tagged and installed.
  PUBLISHED=$(gh release list --repo "$REPO" --limit 200 --json tagName --jq '.[].tagName')
  MISSING=0
  FAILED=''
  for TAG in $(git -C "$PROJECT_DIR" tag -l 'v[0-9]*' --sort=v:refname); do
    if ! printf '%s\n' "$PUBLISHED" | grep -qx "$TAG"; then
      MISSING=$((MISSING + 1))
      BACKFILL=1 "$0" "${TAG#v}" || FAILED="$FAILED $TAG"
    fi
  done
  echo "缺少 Release 的标签：$MISSING 个"
  if [ -n "$FAILED" ]; then
    echo "未能发布：$FAILED" >&2
    exit 1
  fi
  exit 0
fi

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

if command -v shasum >/dev/null 2>&1; then SHA256='shasum -a 256'; else SHA256=sha256sum; fi

WORK=$(mktemp -d)
if [ "${BACKFILL:-}" = 1 ]; then
  # Published after the fact: say when the version really came out (date from CHANGELOG.md).
  DATE=$(sed -n "s/^## \[$VERSION\] - \([0-9-]*\).*/\1/p" "$PROJECT_DIR/CHANGELOG.md" | head -n 1)
  { printf '> 原始发布日期 %s。本页面于 %s 补建，安装包由标签 %s 的代码重新构建。\n\n' \
      "${DATE:-未记录}" "$(date +%Y-%m-%d)" "$TAG"
    cat "$NOTES"; } > "$WORK/notes.md"
  NOTES="$WORK/notes.md"
fi
cleanup() {
  git -C "$PROJECT_DIR" worktree remove --force "$WORK/src" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

echo "从 $TAG 构建发布包（会运行全部检查，约 1 分钟）…"
git -C "$PROJECT_DIR" worktree add -q --detach "$WORK/src" "$TAG"
if [ -f "$WORK/src/scripts/build_package.py" ]; then
  # v0.5.1+: one package, same layout as the repository.
  (cd "$WORK/src" && ./scripts/check.sh >"$WORK/build.log" 2>&1 \
    && python3 scripts/build_package.py --out "$WORK/src/dist" >>"$WORK/build.log" 2>&1) || {
    tail -30 "$WORK/build.log" >&2
    echo "检查或打包失败（可能检查到个人信息），未发布" >&2
    exit 1
  }
  ZIP="$WORK/src/dist/SteadyRoute-v$VERSION.zip"
  (cd "$WORK/src/dist" && $SHA256 "SteadyRoute-v$VERSION.zip" >"SteadyRoute-v$VERSION.zip.sha256")
  ASSETS="$ZIP $ZIP.sha256"
else
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
  ASSETS="$ZIP $ZIP.sha256"
  if [ -f "$WORK/src/scripts/share/build_share.py" ]; then
    (cd "$WORK/src" && python3 scripts/share/build_share.py --out "$WORK/src/dist" >>"$WORK/build.log" 2>&1) || {
      tail -30 "$WORK/build.log" >&2
      echo "分享包构建失败，未发布" >&2
      exit 1
    }
    ASSETS="$ASSETS $WORK/src/dist/SteadyRoute-share-v$VERSION.zip"
  fi
fi

NEWEST=$(git -C "$PROJECT_DIR" tag -l 'v[0-9]*' --sort=-v:refname | head -n 1)
if [ "$NEWEST" = "$TAG" ]; then LATEST=--latest; else LATEST=--latest=false; fi

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release edit "$TAG" --repo "$REPO" --title "SteadyRoute $TAG" --notes-file "$NOTES" "$LATEST"
  # shellcheck disable=SC2086  # asset paths have no spaces (mktemp dir)
  gh release upload "$TAG" --repo "$REPO" --clobber $ASSETS
  echo "已更新 GitHub Release $TAG"
else
  gh release create "$TAG" --repo "$REPO" --verify-tag --title "SteadyRoute $TAG" \
    --notes-file "$NOTES" "$LATEST" $ASSETS
  echo "已发布 GitHub Release $TAG"
fi
