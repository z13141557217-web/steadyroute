## 背景

SteadyRoute 已完成 v0.2.0 发布基础设施。下一阶段需要建立后端、看板和测试共享的统一数据契约与状态机，消除前端根据连接数、分数和失败次数自行推断状态的做法。

固定维护约束以 `docs/MAINTENANCE.md` 为准：稳定优先；不中断健康旧连接；不得无理由增加内存；不新增生产依赖、进程或外部服务；性能和界面变化必须比较 RSS、CPU 与接口响应；看板继续使用同一进程、本机回环和缓存快照。

## 第零阶段结论

- [x] main、GitHub、tag 和生产版本可追溯
- [x] v0.2.0 已发布并完成生产验收
- [x] 完整备份、dry-run、自动回滚和危险路径保护可用
- [x] 生产目录与源码仓库分离
- [x] 18/18 测试和 GitHub CI 通过
- [x] 固定维护约束已纳入仓库

## 本阶段范围

### In Scope

- 统一并版本化状态数据契约
- 持久状态 schema version 与迁移
- 代理组决策状态机
- 节点生命周期状态机
- 结构化事件定义及状态转换去重
- `/api/v1/status` 新接口
- 原 `/api/status` 兼容
- 固定测试样例和契约测试
- 示例 fixture，供独立验收看板使用

### Out of Scope

- 不修改生产看板视觉或交互
- 不实现订阅节点动态筛选的完整运行逻辑
- 不实现日志轮换
- 不部署生产
- 不改变现有选路结果和探测策略
- 不引入依赖、数据库或新进程

## 设计原则

1. 后端决定状态、原因和下一步动作。
2. 前端只展示，不从连接数、分数或失败次数推断状态。
3. 状态使用稳定机器码，并提供中文标题和说明。
4. `null` 表示未知或不存在，不能用 `0` 冒充。
5. 时间统一使用 Unix 时间戳，并提供可读时间。
6. 分数明确方向，例如 `lower_is_better: true`。
7. API 包含 `schema_version`。
8. API、fixture、事件和诊断不得包含订阅 URL、认证信息、代理密码或服务器地址。
9. 看板请求只能读取缓存，不能触发测速、连接扫描或线路切换。
10. 新接口为 `/api/v1/status`；旧 `/api/status` 在兼容窗口继续工作。
11. 状态 schema 和 API schema 分开版本化，迁移必须确定、可测试、可回滚。
12. 未知未来版本不得被旧程序静默覆盖。

## 顶层 API 契约

```json
{
  "schema_version": 2,
  "generated_at": 1789762600,
  "generated_at_iso": "2026-09-19T00:00:00Z",
  "service": {},
  "subscription": {},
  "policies": {},
  "groups": [],
  "nodes": [],
  "events": [],
  "history_summary": {},
  "diagnostics": {}
}
```

## service 契约

至少包括：

```json
{
  "name": "稳航 SteadyRoute",
  "status": "running",
  "started_at": 1789760000,
  "started_at_iso": "2026-09-19T00:00:00Z",
  "uptime_seconds": 2600,
  "controller_connected": true,
  "last_cycle_at": 1789762595,
  "last_cycle_at_iso": "2026-09-19T00:43:15Z",
  "last_cycle_duration_ms": 830,
  "next_cycle_at": 1789762615,
  "state_stale": false,
  "memory_mb": 24.1,
  "version": "0.3.0"
}
```

`state_stale` 必须由后端依据检测策略和最后成功周期计算，不能由前端页面时间推断。

## subscription 契约

至少包括：

```json
{
  "generation": 17,
  "last_refresh_at": 1789762000,
  "last_refresh_at_iso": "2026-09-19T00:33:20Z",
  "candidate_count": 16,
  "added_count": 2,
  "removed_count": 1,
  "warming_count": 1,
  "quarantined_count": 2,
  "changes": []
}
```

只有实际候选集合变化才能增加 `generation`；普通探测不得增加。

## 代理组决策对象

```json
{
  "name": "AI 台湾家宽线路",
  "region": "TW",
  "decision": {
    "code": "handover_pending",
    "severity": "info",
    "title": "当前线路已不是最佳，等待安全回优",
    "detail": "候选线路已连续优胜 2 次，等待达到确认门槛",
    "reason_code": "confirmation_incomplete",
    "updated_at": 1789762600
  },
  "current": {
    "id": "node-id",
    "name": "台湾 Seednet HY2",
    "score": 381,
    "quality": 84,
    "availability_short": 1.0,
    "availability_long": 0.998,
    "latency_ms": 369,
    "jitter_ms": 8
  },
  "target": {
    "id": "target-id",
    "name": "台湾 HINET 家宽02",
    "score": 166,
    "quality": 95
  },
  "confirmation": {"current": 2, "required": 3},
  "handover": {
    "mode": "session_sticky",
    "old_node": "台湾 Seednet HY2",
    "new_node": "台湾 HINET 家宽02",
    "old_connections": 4,
    "grace_remaining_seconds": 28,
    "max_grace_seconds": 300,
    "new_connections_use_target": true
  },
  "timers": {
    "cooldown_remaining_seconds": 0,
    "manual_hold_remaining_seconds": 0,
    "estimated_action_at": 1789762630
  }
}
```

没有目标节点时 `target` 必须是 `null`。

## 代理组状态码

- `stable`
- `candidate_confirming`
- `handover_pending`
- `handover_grace`
- `recovery_observing`
- `cooldown`
- `manual_hold`
- `degraded`
- `failover_now`
- `no_candidate`
- `controller_offline`

每个状态码必须有唯一、稳定的中文标题和解释，前端不得覆盖含义。

正常回优：

```text
stable → candidate_confirming → handover_pending → handover_grace
→ recovery_observing → stable
```

故障切换：

```text
stable → degraded → failover_now → recovery_observing → stable
```

异常分支：

```text
任意状态 → controller_offline
任意状态 → no_candidate
任意可优化状态 → manual_hold
```

## 节点生命周期

- `discovered`
- `warming`
- `healthy`
- `degraded`
- `quarantined`
- `half_open`
- `retired`

```text
discovered → warming → healthy → degraded → quarantined
→ half_open → healthy
```

订阅消失：

```text
任意状态 → retired → 24 小时后清理
```

节点对象至少包括：

```json
{
  "id": "stable-ui-id",
  "name": "台湾 HINET 家宽02",
  "group": "AI 台湾家宽线路",
  "region": "TW",
  "lifecycle": "healthy",
  "role": "best_candidate",
  "transport": "unknown",
  "availability_short": 1.0,
  "availability_long": 0.998,
  "latency_ewma_ms": 82,
  "latency_p95_ms": 105,
  "jitter_ms": 6,
  "score": 166,
  "lower_score_is_better": true,
  "success_streak": 18,
  "failure_streak": 0,
  "sample_count_short": 20,
  "sample_count_long": 760,
  "last_probe_at": 1789762590,
  "next_probe_at": 1789762710,
  "quarantine_until": null,
  "warmup_progress": null
}
```

节点 UI ID 由代理组与节点显示名称的非敏感稳定哈希生成。节点改名视为新节点，不继承旧节点健康历史。

## 结构化事件

- 每次真实状态转换产生事件。
- 普通轮询和相同状态重复不得产生重复事件。
- 事件包含稳定 code、severity、group/node ID、旧状态、新状态、reason_code、时间。
- 事件不得包含秘密。
- 本阶段定义事件契约和固定 fixture；事件持久化容量策略可在后续阶段实现。

## 迁移与兼容

- 为当前 `state.json` 定义 schema version。
- 提供至少 v1 → v2 的确定性迁移。
- 迁移前原子备份旧状态。
- 迁移失败保留旧文件并安全重建，不得半写入。
- 未知未来版本拒绝写回。
- `/api/status` 保持当前消费者可用；`/api/v1/status` 使用新契约。
- 新接口与旧接口必须来自同一缓存快照。

## 固定验收样例

为下列场景提供 JSON fixture、状态转换测试和唯一中文文案验证：

- 当前最佳
- 候选确认 1/3、2/3、3/3
- 等待安全交接
- 正在交接
- 交接完成
- 当前节点性能下降
- 当前节点故障
- 无候选
- 手动锁定
- 控制器断开和恢复
- 新节点发现与预热
- 节点进入隔离
- 半开放恢复
- 节点退役
- null 与 0 的区分
- 状态过期

## 验收条件

- [ ] 设计文档先于实现完成并通过审查。
- [ ] 后端状态码和文案有单一映射表。
- [ ] 前端文件没有新增判断公式或状态推断。
- [ ] `/api/v1/status` schema 契约测试通过。
- [ ] `/api/status` 兼容测试通过。
- [ ] 新旧接口来自同一缓存，不触发探测或连接读取。
- [ ] v1 → v2 状态迁移、损坏恢复、未知未来版本测试通过。
- [ ] 所有固定样例均能唯一映射状态和中文说明。
- [ ] 无秘密字段进入 API、fixture、日志或文档。
- [ ] 修改前后 RSS、空闲 CPU、接口响应和 100 次刷新压测有对比；不得有无理由回退。
- [ ] 提供独立验收看板，展示全部 fixture 和状态转换；不修改生产看板。
- [ ] 不部署生产，不合并 PR，等待管理窗口验收。

## 版本建议

本阶段完成后建议进入 `v0.3.0` 候选；是否合并和发布由管理验收决定。
