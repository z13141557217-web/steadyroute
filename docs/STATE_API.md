# 状态、状态机与只读 API 契约

本文是 Issue #4 的实现级契约；完整范围与验收清单保存在
`.github/ISSUE_DRAFTS/004-versioned-state-api.md`。

## 版本边界

- `state.json` 当前 `schema_version` 为 `2`。
- `/api/v1/status` 当前响应 `schema_version` 为 `2`；URL 版本与响应 schema 独立。
- 缺少 `schema_version` 或旧 `version: 1` 的状态视为 v1，确定性迁移到 v2。
- 迁移写回前原子保存 `state.v1-backup.json`。
- JSON 损坏保存为 `state.corrupt-<unix>.json`；结构迁移失败保存为
  `state.migration-failed-<unix>.json`，随后原子写入安全空状态。
- 高于 `2` 的未知未来版本抛出错误，原文件保持逐字节不变，禁止旧程序写回。

## 后端唯一事实源

后端为每个代理组输出 `decision`，为每个节点输出 `lifecycle` 与
`lifecycle_status`。机器码、严重性、原因码、下一动作和中文标题/说明均来自后端唯一
映射表。页面可以决定颜色和排版，但不能根据分数、连接数、失败次数或时间重新判断
状态。未知/不存在使用 `null`，真实计数或剩余时间为零才使用 `0`。

代理组机器码：`stable`、`candidate_confirming`、`handover_pending`、
`handover_grace`、`recovery_observing`、`cooldown`、`manual_hold`、`degraded`、
`failover_now`、`no_candidate`、`controller_offline`。

节点生命周期：`discovered`、`warming`、`healthy`、`degraded`、
`quarantined`、`half_open`、`retired`。

正常回优保持会话粘滞：新请求使用目标线路，健康旧连接自然结束；只有
`failover_now` 对应的真实故障路径允许清理失效旧连接。状态投影不参与候选选择，因而
不改变现有选路结果、探测频率、短/长期健康窗口或隔离策略。

确认达到 3/3 时执行器仍在当前周期立即切换，不为了看板延迟动作。若一个周期跨过
多个解释状态，事件按声明路径记录：有健康旧连接时为
`candidate_confirming → handover_pending → handover_grace`；没有旧连接时继续记录零时长
`handover_grace → recovery_observing`。最终 API 快照只显示周期结束时的真实当前状态。
观察期结束后若性能冷却仍有效，实际路径为 `recovery_observing → cooldown → stable`。
直接调用事件接口时，任何未声明边都抛出错误；状态投影只能通过显式中间路径归一化。

## API

`GET /api/v1/status` 顶层顺序和字段为：

```text
schema_version, generated_at, generated_at_iso, service, subscription,
policies, groups, nodes, events, history_summary, diagnostics
```

时间字段同时提供 Unix 秒与 `_iso` UTC 形式。节点 `score` 总是附带
`lower_score_is_better: true`，策略中再次声明方向与单位。`target` 不存在时为
`null`。响应、事件、订阅变化和 fixture 使用字段白名单，禁止订阅 URL、认证信息、
密码和服务器地址。

`GET /api/status` 在兼容窗口保留原 `service`、`policy`、`groups`、`nodes` 形状，并
增加后端状态字段。两个 API 在周期结束时一次生成、一次编码，带相同 `snapshot_id`；
HTTP 请求只读取缓存。缓存尚未生成时返回 `503 snapshot_unavailable`，绝不以同步探测
补齐。

v0.4.0 在 `subscription.dynamic` 增加兼容字段，响应 schema 仍为 2。它包含 `mode`、
`generation` 和逐策略的上一代/当前/提议计数、静态/动态差异、group status、当前节点
存在性、fail-closed、节点生命周期、预热进度、疑似改名与退役倒计时。该投影与旧/新
API 一起从周期缓存编码；访问 `/candidate-acceptance` 仍不会读取控制器或触发测速。

持久状态继续使用 schema 2 的兼容扩展 `candidate_registry`。v0.3.0 会保留未知扩展字段，
因此 shadow 回滚不会破坏原状态；未来若出现不兼容结构变化再递增持久 schema。

## 结构化事件

代理组决策码或节点生命周期真实变化时追加一条事件，相同状态重复投影不会追加。
事件包含 `code`、`severity`、`scope`、group/node ID、旧状态、新状态、
`reason_code`、Unix 时间和 ISO 时间。当前实现保留最近 200 条以维持既有有界状态
不变量；日志轮换仍属于后续阶段。

候选事件使用稳定 ID 和白名单字段，支持 `SUBSCRIPTION_CHANGED`、`NODE_DISCOVERED`、
`NODE_WARMUP_STARTED`、`NODE_WARMUP_COMPLETED`、`NODE_REMOVED`、`NODE_RETIRED`、
`NODE_RETIREMENT_PURGED`、`CURRENT_NODE_REMOVED`、`GROUP_MISSING`、`GROUP_RECOVERED`、
`NO_CANDIDATE` 和 `CANDIDATES_RECOVERED`，不包含订阅 URL、服务器地址或凭据。

## 固定验收

`src/steadyroute/fixtures/status_contract_v2.json` 固定覆盖全部状态码、全部生命周期、
候选确认 1/3 至 3/3、交接、故障、无候选、手动保持、控制器恢复、`null`/`0` 和
状态过期。候选服务运行后访问 `http://127.0.0.1:17654/acceptance` 可查看全部 fixture
及转换路径；该页面只读、同进程、本机回环，不替换 `/` 的生产看板。
每个样例同时标记为“真实运行快照可达”“同周期瞬时事件”或“契约边界模拟”，避免把
瞬时状态误解为生产一定会停留的最终快照。

## 回滚

本阶段不部署生产。若后续管理窗口验收失败，使用 `rollback-local.sh --apply` 恢复
v0.2.0 的完整目录与发布前状态。不得只替换 Python 文件，也不得让旧版本覆盖未知
schema。性能回退时优先恢复旧实现并保留本机诊断副本。
