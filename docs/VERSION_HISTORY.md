# 版本历史与发布验收

更新时间：2026-09-22。本文件记录每版**实际交付、验收、限制和回滚**。
[变更日志](../CHANGELOG.md)只作简要索引，[项目状态](PROJECT_STATUS.md)只记当前门槛。
Git tag、GitHub Release、生产部署是三项独立事实，不能互相推断。
后续每版须随发布 PR 更新本文件，并在生产验收后补记结果；不得把计划写成已交付。

| 版本 | 日期 | 代码 / 审查 | 发布与生产状态 |
|---|---|---|---|
| v0.1.0 | 2026-09-19 | `18fdcee`；初始基线 | 有 tag；未找到 GitHub Release，早期生产验收记录不完整 |
| v0.2.0 | 2026-09-19 | `1e7ef80`；[PR #7](https://github.com/z13141557217-web/steadyroute/pull/7) | 已发布并曾部署生产，现已被后续版本取代 |
| v0.3.0 | 2026-09-20 | `60ea623`；[PR #8](https://github.com/z13141557217-web/steadyroute/pull/8) | 已发布并曾部署生产，现已被后续版本取代 |
| v0.4.0 | 2026-09-20 | `27010e9`；[PR #10](https://github.com/z13141557217-web/steadyroute/pull/10) | 已发布并曾部署生产；动态发现仅影子运行 |
| v0.4.1 | 2026-09-21 | `a833625`；[PR #12](https://github.com/z13141557217-web/steadyroute/pull/12) | 已发布并曾部署生产；动态发现仍仅影子运行 |
| v0.4.2 | 2026-09-21 | `082e8f6`；[PR #14](https://github.com/z13141557217-web/steadyroute/pull/14) | 已发布；2026-09-22 核对时生产发布清单为 v0.4.2，动态发现仍仅影子运行 |

“测试通过”只指该版本发布时的检查，不替代长期生产观察或真实订阅刷新验证。
生产备份根目录为 `/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router Backups`。
恢复必须遵循[部署手册](DEPLOYMENT.md)，不得手工混装不同版本文件。

## v0.4.2 — 隐藏发现组与限时人工偏好

- **关联：**[Issue #13](https://github.com/z13141557217-web/steadyroute/issues/13)、[PR #14](https://github.com/z13141557217-web/steadyroute/pull/14)、[Release](https://github.com/z13141557217-web/steadyroute/releases/tag/v0.4.2)。
- **实际改变：**内部发现组设为 `hidden: true`，仍供 Mihomo API 读取；人工选择保护最多一小时，仅暂停普通性能回优。故障、候选消失、无安全候选和业务预检不受人工偏好阻挡；升级时封顶旧状态的过长保持期。
- **测试与生产验收：**仓库检查、CI 和人工偏好/发现组回归测试通过；生产 API 此前曾核对为 v0.4.2、控制器连接且状态新鲜；本次文档核对只确认生产发布清单仍为 v0.4.2，未重新取得 API 健康响应。至少 12–24 小时及**本版发布后一次真实订阅刷新**尚无完整验收记录，不能标记完成。
- **已知限制：**动态候选仍为 `shadow`，静态候选实际选路；Claude 地区提示的原因不能由此版本证明。
- **备份与回滚：**部署前备份 `20260921T093834Z-0.4.1-a83362534c0c`；完整回滚到 v0.4.1。发布包 SHA-256：`f65f00a7328a3f21db25cdbe6d4ad586ef88cea3e8e58d1193f9aa11797a4e6e`。

## v0.4.1 — Clash Verge 控制面兼容

- **关联：**[PR #12](https://github.com/z13141557217-web/steadyroute/pull/12)、[Release](https://github.com/z13141557217-web/steadyroute/releases/tag/v0.4.1)。
- **实际改变：**兼容 Clash Verge 2.5.4 服务模式的新 Mihomo Unix socket 路径；解析控制器 `Content-Length` 与 chunked 响应并拒绝畸形/超限响应；group enhancement 重载前保存活动组选择，重载后以 dry-run 优先、显式 apply 的事务方式安全回放。
- **测试与生产验收：**发布检查和控制器兼容/响应解析测试通过；新 socket 下连接恢复，组选择回放纳入发布流程。
- **已知限制：**不等于升级后自动恢复全部 Clash Verge 配置；动态候选仍为 `shadow`。
- **备份与回滚：**部署前备份 `20260920T164636Z-0.4.0-27010e97844d`；完整回滚到 v0.4.0。发布包 SHA-256：`5ebc0d5c5b88a246df5ff202ccc0d41f38fd5617457609746e00a16dfb25975b`。

## v0.4.0 — 动态发现的影子运行

- **关联：**[Issue #2](https://github.com/z13141557217-web/steadyroute/issues/2)、[PR #10](https://github.com/z13141557217-web/steadyroute/pull/10)、[Release](https://github.com/z13141557217-web/steadyroute/releases/tag/v0.4.0)。
- **实际改变：**策略 JSON 驱动候选筛选，Mihomo 原生发现组与空组 `REJECT` 保护；两次成功快照确认增删改名，节点预热、24 小时退役、有界事件与只读影子验收页；配置生成、订阅绑定、备份和回滚工具。
- **测试与生产验收：**筛选、假控制器、生命周期、缺组/空组、不同节点规模及无 PUT/DIRECT 泄漏测试通过；发布后以独立影子看板观察，不改变静态选路。
- **已知限制：**发布包强制拒绝 `mode=active`；真实订阅刷新及资源门槛仍须在接管前单独证明。
- **备份与回滚：**部署前备份 `20260920T112818Z-0.3.0-60ea623e97b5`；完整回滚到 v0.3.0。发布包 SHA-256：`48fba6e0d86a2123e4819186069ad146bb46db680c195c6a110ead7372b808a1`。

## v0.3.0 — 统一状态契约

- **关联：**[Issue #4](https://github.com/z13141557217-web/steadyroute/issues/4)、[PR #8](https://github.com/z13141557217-web/steadyroute/pull/8)、[Release](https://github.com/z13141557217-web/steadyroute/releases/tag/v0.3.0)。
- **实际改变：**持久状态 schema v2 的备份迁移与损坏隔离；组决策状态机、节点生命周期、去重结构化事件；`/api/v1/status` 和兼容的 `/api/status` 共用缓存快照；固定 fixture 和只读验收页。
- **测试与生产验收：**迁移、契约、兼容、缓存只读、安全字段、状态转换与回优链路测试通过；生产通过发布健康门槛后上线。
- **已知限制：**尚无动态候选和应用级日志轮换；有界事件不等于长期历史存储。
- **备份与回滚：**部署前备份 `20260919T184254Z-0.2.0-1e7ef80d8865`；完整回滚须恢复发布前状态，旧版不可直接读取 schema v2。发布包 SHA-256：`96a7bebe6f15ecf2593f0247834300a9921990a21768c458a0156d073e51ae9d`。

## v0.2.0 — 可回滚的本地发布

- **关联：**[Issue #1](https://github.com/z13141557217-web/steadyroute/issues/1)、[PR #7](https://github.com/z13141557217-web/steadyroute/pull/7)、[Release](https://github.com/z13141557217-web/steadyroute/releases/tag/v0.2.0)。
- **实际改变：**默认 dry-run、显式 apply 的原子部署；部署前全量备份与 SHA-256；发布包清单与暂存校验；LaunchAgent、API、代理组、当前节点与新鲜度健康门槛；失败自动恢复和独立回滚命令。
- **测试与生产验收：**临时目录部署、故障注入、恢复与回滚集成检查通过；发布后曾成为生产基线。
- **已知限制：**仍是静态候选和旧状态契约；日志增长问题未解决。
- **备份与回滚：**本版引入标准机制；更早的部署前备份编号未核实，不宣称可恢复 v0.1.0。发布包 SHA-256：`7155a857493ced7b3c8abcf0b6baec6fa2071f14cedf968458821874fd0af6f1`。

## v0.1.0 — 受管源码基线

- **关联：**Git tag `v0.1.0`；未找到同名 GitHub Release。
- **实际改变：**导入既有加权选路器、本地看板、台湾/香港增强配置；建立 6 个初始测试、文档、检查脚本、发布包构建、提交门禁和 CI。
- **测试与生产验收：**可确认仓库基线和初始测试；未找到正式 Release 资产和完整生产验收记录，不能补写为“已正式发布”。
- **已知限制：**没有原子部署/回滚、版本化状态、动态候选和日志轮换。
- **备份与回滚：**尚不具备 v0.2.0 的标准完整目录备份与回滚能力。
