# 运维手册

## 日常检查

运行：

```bash
./scripts/status.sh
```

关注：

- LaunchAgent 是否 running。
- 最近周期是否及时。
- 当前台湾/香港节点是否属于候选集。
- 是否存在长期隔离或空候选。
- 日志是否接近上限。
- RSS、CPU 和本地接口响应是否异常。
- `/api/v1/status` 的 `service.state_stale`、`controller_connected` 和 `diagnostics.snapshot_id`。
- `/candidate-acceptance` 的 generation、静态/动态差异、warming、retired、group status
  与 fail-closed；它只读缓存，不代表已接管。

## 动态候选影子告警

- `GROUP_MISSING` 或控制器 offline：保留最后集合，禁止手工清空状态；等待恢复并确认
  `GROUP_RECOVERED`。
- 单次空组：属于 pending，不处置为删除；连续成功空快照才形成 `NO_CANDIDATE`。
- `CURRENT_NODE_REMOVED`：影子模式只检查成熟同策略建议，不执行 PUT。
- `NO_CANDIDATE`：确认 Mihomo 组仍是 `empty-fallback: REJECT`；不得改成 DIRECT 或
  COMPATIBLE 临时恢复。
- 误收/漏收：保持 shadow，修正 `route-policies.json`，重新生成并 staged 校验后再观察。

## discovery group 上线检查

应用部署和 Clash Verge group enhancement 是两个独立事务。`manage-clash-groups.py` 从
注入的 `profiles.yaml` 读取 current remote subscription，再读取其 `option.groups`，不会
猜测或写死 UID。它只允许同级、非符号链接、名为 `profiles` 的明确目录中的已有绑定文件。

- `deploy` 与 `rollback` 默认只输出计划；写入必须显式 `--apply`。
- apply 前确认生成文件与 `route-policies.json` 一致、模式仍为 shadow，并用指定 Mihomo
  核心校验临时独立配置。
- apply 先在 `profiles/.steadyroute-group-backups/` 保存原件、SHA 和元数据，再同目录原子
  替换；替换后校验失败会自动恢复。
- 本工具不自动重载 Clash Verge。人工重载后运行：

```bash
python3 scripts/verify-clash-discovery.py --socket "/tmp/verge/verge-mihomo.sock"
```

只有输出 `"ready": true` 且列出所有策略 discovery group 时才能开始影子观察。若仍有
`GROUP_MISSING`，停止计时，不得把它当作动态筛选结果。

## 状态 schema 故障

- `state.v1-backup.json` 是 v1→v2 前的原子备份，不提交 Git。
- `state.corrupt-<unix>.json` 或 `state.migration-failed-<unix>.json` 是本机诊断副本，
  不得复制到日志或看板。
- 日志出现 `unsupported future state schema` 时停止用旧版本启动；不要删除或改写状态，
  应恢复能识别该版本的程序或使用完整部署备份回滚。
- 从 v0.3.0 候选回滚 v0.2.0 时使用完整目录备份，禁止把 v2 状态手工拼接到旧版本。

## 故障等级

| 等级 | 示例 | 处置 |
|---|---|---|
| SEV-1 | DIRECT 泄漏、所有候选为空、错误地区出口 | 立即停止自动切换并回滚 |
| SEV-2 | 服务停止、控制器持续离线、错误节点切换 | 15 分钟内恢复或回滚 |
| SEV-3 | 看板不可用、日志异常增长、单节点误判 | 当日修复 |
| SEV-4 | 文案、布局和非关键诊断问题 | 正常迭代 |

## 事件响应

1. 记录发生时间和用户症状。
2. 保存状态、最近事件和有限日志片段。
3. 确认是 Mihomo、节点、稳航还是看板问题。
4. 优先恢复服务，避免同时改变多个变量。
5. 验证恢复。
6. 建立回归测试和复盘行动项。

## 备份策略

- 每次生产发布前创建版本化备份。
- 保留最近 5 个正式版本和最近 2 个已验证稳定版本。
- 临时实验备份在发布完成后归档或清理。
- 每个备份包含版本、commit、时间和校验和。
- 订阅与凭据备份必须保持用户权限，不能进入 Git。

## 发布失败处置

部署脚本在关键检查失败后自动恢复切换前完整目录和 LaunchAgent plist，并重新运行冒烟
检查。先保留备份根目录 `diagnostics/` 中的失败版本与原因，再运行
`./scripts/status.sh`。需要人工选择稳定备份时，先运行 `./scripts/rollback-local.sh`
预览，再以 `--apply` 执行。不要从不同备份手工拼接文件。
