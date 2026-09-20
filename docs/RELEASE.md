# 发布与版本管理

## 环境

| 环境 | 说明 |
|---|---|
| Development | Git 工作目录，允许测试和修改 |
| Candidate | 从固定 commit 构建的发布包 |
| Production | `Library/Application Support` 中的实际运行版本 |

## 版本来源

版本由以下内容共同确定：

- `VERSION`
- Git tag，例如 `v0.1.0`
- `CHANGELOG.md`
- 发布包清单及 SHA-256

禁止用 zip 文件名代替正式版本。

## 发布流程

1. 确保工作树干净。
2. 运行 `./scripts/check.sh`。
3. 更新 `VERSION` 与 `CHANGELOG.md`。
4. 创建发布 commit 和 tag。
5. 运行 `./scripts/build-release.sh`。
6. 生成生产备份和校验和。
7. 先部署后端和看板到暂存路径并验证语法。
8. 原子替换生产文件。
9. 重启 LaunchAgent。
10. 执行生产冒烟测试。
11. 记录部署版本、时间、结果和备份位置。

上述第 6 至 11 步由 `./scripts/deploy-local.sh` 执行。命令默认 dry-run；真实执行使用
`--apply`。生产 apply 还要求当前 commit 带 `v<VERSION>` 标签，且首次生产写入必须
交互式再次确认。完整命令和备份格式见 [DEPLOYMENT.md](DEPLOYMENT.md)。

同版本发布包先写入 `dist/` 下的随机临时 zip 与校验文件，完成后再分别原子重命名为
正式产物。重复构建不会向旧 zip 追加成员；包内 manifest 仍要求文件集合完全一致，
任何残留或重复内容都会在部署暂存校验中被拒绝。

## 回滚触发条件

- 服务无法持续运行。
- Mihomo 配置不能加载。
- 代理组丢失或回落 DIRECT。
- 状态文件无法读取。
- 选路行为与发布前 dry-run 不一致。
- RSS、CPU 或接口延迟明显回退。
- 状态迁移失败、未来 schema 拒绝或新旧 API 快照不一致。
- 动态筛选误收普通/信息节点、漏收现有家宽或出现 DIRECT/跨地区建议。
- RSS 相对 v0.3.0 增量超过 1 MB，或策略包不再是 `mode=shadow`。

## 回滚原则

回滚恢复上一个完整版本，不在生产目录临时拼接多个版本的文件。回滚后再次执行生产冒烟测试，并保留故障版本日志用于复盘。

标准命令为 `./scripts/rollback-local.sh`（预演）和
`./scripts/rollback-local.sh --apply`（执行）。

`v0.3.0` 候选首次启动会把 v1 `state.json` 原子备份为 `state.v1-backup.json` 后写入
schema v2。标准完整目录回滚会恢复发布前状态；不要让 v0.2.0 手工复用候选目录中的
v2 状态。未知未来 schema 必须保留原文件并恢复能识别它的版本。

## v0.4.0 影子发布约束

本 PR 只构建候选包，不创建 tag、不部署。候选包校验会拒绝 `mode=active`。后续管理窗口
如批准 v0.4.0，应先 dry-run，再观察 12–24 小时并覆盖一次真实订阅刷新；验收
`/candidate-acceptance` 的静态/动态差异、预热、退役和资源指标。正式接管必须另开
v0.4.1 审批，不能通过修改运行目录绕过策略、RSS 和发布门禁。

回滚目标为完整的 v0.3.0 / `60ea623` 包与部署前状态。`candidate_registry` 是 schema 2
兼容扩展，旧版本会保留该字段；仍应使用完整目录回滚，不手工混装 Python 文件。
