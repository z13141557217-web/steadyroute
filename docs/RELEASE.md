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

## 回滚原则

回滚恢复上一个完整版本，不在生产目录临时拼接多个版本的文件。回滚后再次执行生产冒烟测试，并保留故障版本日志用于复盘。

标准命令为 `./scripts/rollback-local.sh`（预演）和
`./scripts/rollback-local.sh --apply`（执行）。
