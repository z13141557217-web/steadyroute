#!/bin/sh
set -eu

REPO='z13141557217-web/steadyroute'
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DRAFT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

ensure_label() {
  name=$1
  color=$2
  description=$3
  gh label create "$name" --repo "$REPO" --color "$color" --description "$description" --force
}

issue_exists() {
  title=$1
  gh issue list --repo "$REPO" --state all --limit 200 --json title --jq '.[].title' | grep -Fqx "$title"
}

publish() {
  title=$1
  labels=$2
  body_file=$3
  if issue_exists "$title"; then
    echo "Already exists: $title"
    return
  fi
  gh issue create --repo "$REPO" --title "$title" --label "$labels" --body-file "$body_file"
}

ensure_label 'priority: P0' 'b60205' 'Must be completed before expanding production changes'
ensure_label 'priority: P1' 'd97706' 'High-priority planned work'
ensure_label 'priority: P2' 'fbca04' 'Important but not blocking current safety'
ensure_label 'type: feature' '1d76db' 'New user-facing or system capability'
ensure_label 'type: maintenance' '6e7781' 'Engineering, operations, tests, docs, or technical debt'
ensure_label 'area: deploy' '5319e7' 'Build, release, deployment, and rollback'
ensure_label 'area: routing' '0e8a16' 'Candidate discovery and routing decisions'
ensure_label 'area: observability' '0052cc' 'Logs, events, metrics, and diagnostics'
ensure_label 'area: state-api' 'bfdadc' 'Persistent state and local API contracts'
ensure_label 'area: dashboard' 'd4c5f9' 'Dashboard behavior and presentation'
ensure_label 'area: testing' 'c2e0c6' 'Automated and integration testing'

publish '[P0] 建立本地原子部署与自动回滚流程' 'priority: P0,type: maintenance,area: deploy' "$DRAFT_DIR/001-atomic-deploy.md"
publish '[P0] 动态识别家宽候选并实现空候选保护' 'priority: P0,type: feature,area: routing' "$DRAFT_DIR/002-dynamic-candidates.md"
publish '[P0] 实现有界日志轮换与看板断开错误边界' 'priority: P0,type: maintenance,area: observability' "$DRAFT_DIR/003-bounded-logging.md"
publish '[P1] 建立状态 schema 迁移与版本化 API' 'priority: P1,type: feature,area: state-api' "$DRAFT_DIR/004-versioned-state-api.md"
publish '[P1] 建立可解释决策状态与新版数据看板' 'priority: P1,type: feature,area: dashboard' "$DRAFT_DIR/005-explainable-dashboard.md"
publish '[P1] 建立 Mihomo Unix Socket 集成测试' 'priority: P1,type: maintenance,area: testing' "$DRAFT_DIR/006-mihomo-integration-tests.md"
