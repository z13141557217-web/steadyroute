# 稳航 SteadyRoute

稳航是运行在 macOS 上的 Clash Verge/Mihomo 家宽线路控制器。它负责台湾与香港住宅线路的健康检测、故障切换、无损回优、节点隔离和本地只读状态看板。

## 仓库定位

本仓库是唯一开发源。正在运行的目录只是部署目标：

- 源码仓库：`/Users/nurture/Projects/steadyroute`
- 生产目录：`/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router`
- 生产日志：`/Users/nurture/Library/Logs/Clash-Verge-Stability-Router`
- LaunchAgent：`/Users/nurture/Library/LaunchAgents/com.nurture.clash-stability-router.plist`

禁止直接在生产目录开发。紧急修复后必须立即反向同步到仓库并补测试、变更日志和版本记录。

## 目录

```text
src/steadyroute/        后端与本地看板
tests/                  单元、集成与回归测试
config/clash-verge/     可纳入版本控制的 Clash Verge 增强配置
deploy/macos/           macOS LaunchAgent 模板
scripts/                检查、构建、状态与后续部署脚本
docs/                   产品、架构、运维、安全、发布与风险文档
```

## 常用命令

```bash
./scripts/check.sh
./scripts/status.sh
./scripts/build-release.sh
./scripts/deploy-local.sh
./scripts/rollback-local.sh
python3 scripts/manage-clash-groups.py --help
python3 scripts/verify-clash-discovery.py --help
```

`deploy-local.sh` 和 `rollback-local.sh` 默认只预演。真实写入必须显式传入
`--apply`；第一次生产部署还必须在交互终端再次输入完整确认短语。详见
[本地部署与回滚](docs/DEPLOYMENT.md)。

Clash Verge 当前订阅的 group enhancement 是独立控制面；管理命令同样默认 dry-run，
必须显式注入 `profiles.yaml`、`profiles/` 和 Mihomo core。文件 apply 后仍需人工重载
Clash Verge，并通过 `/proxies` 只读验证 discovery groups，详见[发布流程](docs/RELEASE.md)。

## 版本策略

采用语义化版本：

- PATCH：兼容性修复、文案和低风险调整。
- MINOR：兼容的新能力、状态字段或看板功能。
- MAJOR：状态格式、配置结构或部署方式的不兼容变化。

在 `1.0.0` 前，任何状态结构和部署契约仍可能调整，但每次调整都必须有迁移与回滚说明。

## 当前基线

开发基线是已发布的 `v0.4.1`。当前维护线准备 `v0.4.2`
动态候选影子候选；合并、标签、生产部署和动态接管仍需分别经过管理窗口验收。

版本化状态与只读 API 见[状态契约](docs/STATE_API.md)。固定样例可由候选进程在
`http://127.0.0.1:17654/acceptance` 独立验收，不替换生产看板视觉。
动态候选影子状态在 `http://127.0.0.1:17654/candidate-acceptance` 独立只读展示。

## 教程

- [GitHub 私有仓库初始化](docs/GITHUB_SETUP.md)
- [日常开发完整流程](docs/DAILY_WORKFLOW.md)
