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
```

## 版本策略

采用语义化版本：

- PATCH：兼容性修复、文案和低风险调整。
- MINOR：兼容的新能力、状态字段或看板功能。
- MAJOR：状态格式、配置结构或部署方式的不兼容变化。

在 `1.0.0` 前，任何状态结构和部署契约仍可能调整，但每次调整都必须有迁移与回滚说明。

## 当前基线

`v0.1.0` 是把现有生产实现纳入正式工程管理的首个基线版本，不代表功能从零开始。

