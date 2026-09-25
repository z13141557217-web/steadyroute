# 架构说明

## 系统边界

```text
Clash Verge/Mihomo
  ├─ 代理组与分流规则
  ├─ Unix Socket 控制 API
  └─ 实际连接
           │
           ▼
SteadyRoute 后端
  ├─ 策略配置与 Mihomo 组生成
  ├─ 动态候选注册表（v0.4.0 shadow）
  ├─ 探测调度
  ├─ 健康模型
  ├─ 决策状态机
  ├─ 原子状态持久化
  ├─ 有界事件/日志
  └─ 只读缓存 API
           │
           ▼
本地数据看板（127.0.0.1）
```

## 目标模块边界

当前后端仍是单文件实现。后续按测试保护逐步拆分：

| 模块 | 责任 |
|---|---|
| `controller_client` | Mihomo Unix Socket API |
| `candidate_registry` | 动态候选发现和生命周期 |
| `route_policy` | 单一 JSON 策略加载、验证、筛选与 Mihomo 组生成 |
| `probe_scheduler` | 自适应基础/业务探测 |
| `health_model` | 短期、长期、隔离与恢复 |
| `decision_engine` | 故障切换、无损回优和手动保持 |
| `state_store` | schema、迁移、原子保存与损坏恢复 |
| `event_store` | 固定长度结构化事件 |
| `http_api` | 只读、缓存、版本化本地 API |
| `logging_setup` | 有界日志和异常入口 |

拆分原则：先补边界测试，再移动代码；不得为“文件更漂亮”制造运行风险。

## 动态候选影子数据流

```text
route-policies.json
  ├─ 生成/校验 Mihomo active + discovery groups
  └─ 驱动 SteadyRoute policies
             │
discovery group.all（仅成功且组存在）
  → 连续两次相同差异确认
  → generation / added / removed
  → discovered → warming → healthy / … / retired
  → shadow 差异与建议（不执行 PUT）
```

`route_policy` 以一个标准 JSON 接口隐藏正则兼容性、安全下限、组生成和激活预算。
`candidate_registry` 的 seam 是一次不可变 `/proxies` 快照；离线、缺组和畸形响应不进入
差异确认。台湾和香港只存在于策略数据中，核心模块不按地区或组名分支。

`mode=shadow` 时 `evaluate_group` 只接收静态候选。未来 `mode=active` 才会接收已成熟的
动态候选，且策略验证要求单独审批与 RSS ≤1 MB；v0.4.0 发布校验拒绝 active 包。

仓库侧 `clash_group_deploy` 控制面是独立模块：它从 `profiles.yaml` current subscription
解析 `option.groups`，对受限 profiles 目标执行 dry-run/备份/原子替换/自动恢复/回滚。
该模块随候选包提供但不复制到稳航运行目录，也不成为常驻依赖。Clash Verge 重载保持
显式人工动作，随后由只读 `/proxies` 验证 discovery groups 是否真正加载。

## 版本化状态投影

`state_contract.py` 负责持久状态 schema、确定性迁移、决策/生命周期文案和转换事件；
现有选路函数仍是执行行为的唯一入口。每轮探测和选路结束后，后端从同一份内存状态
投影一次缓存快照，并同时序列化 `/api/status` 兼容视图和 `/api/v1/status` v2 视图。
HTTP GET 只读取已编码字节，不连接 Mihomo、不扫描连接，也不触发切换。

持久状态和 API 独立版本化。v1 状态迁移前保存原子备份；损坏状态保存诊断副本后
以安全空状态重建；高于当前版本的状态立即拒绝，旧进程不能写回。详见
[状态契约](STATE_API.md)与 [ADR-0004](adr/0004-versioned-state-contract.md)。

## 探测调度（v0.4.3）

```text
每 20 秒固定排期（单调时钟）
  → 休眠识别（先于任何探测）
  → 一次并行批次：当前节点、热备、轮询备用、预热；到期时当前与热备同 URL 业务探测
  → 并行重试失败的业务探测一次
  → 业务差分：当前失败 + 热备成功 = 节点故障；两者都失败 = 目标站点故障
  → 当前基础失败：同周期复测 2 次 + DIRECT 只读本机网络检测
  → 本机断网或唤醒首轮：只记录成功样本，跳过决策
  → 选路决策 → 完成时间写入 updated_at → 缓存快照
```

正常周期耗时约等于最慢一次探测（≤ 3 秒）；只有真实故障时才追加复测和预检。

探测共用一个常驻线程池（最多 10 线程）。两次完整周期之间，快速通道只把实时字段补丁到
最近一次完整快照上再编码，不重建快照。

## 日志（v0.4.4）

`logging_setup.py` 在进程拿到单实例锁之后安装三个处理器：`router.log`（INFO，突发限频）、
`router-error.log`（WARNING 以上，10 分钟去重）、`events.jsonl`（独立 logger，只收决策
记录）和 `node-events.jsonl`（节点状态变化，30 天）。每个处理器按本地日期和单文件上限轮换、gzip 历史、按天数与总量清理。选路代码只
调用 `log`/`log_warning`/`log_error`/`log_routine` 与 `logging_setup.write_event`，不直接
接触文件。`--once` 与交互运行输出到标准输出，不写文件。

## 数据所有权

- Git：源码、测试、模板、文档和发布元数据。
- `state.json`：生产运行状态，不进入 Git。
- 日志：诊断信息，有界保留，不作为业务状态来源。
- 订阅：外部输入，不进入 Git。
- 发布包：由 Git commit 和版本构建，可重建。

## 关键不变量

1. 看板刷新不能触发测速或路由切换。
2. 普通性能回优不得主动中断健康旧连接。
3. 真实故障才允许清理失效连接。
4. 没有安全候选时不得静默回落 DIRECT。
5. 状态、事件和趋势数据必须有容量上限。
6. 生产部署必须来自已提交、已标记、已验证的版本。
7. 发现失败不得等价为空集合；只有确认的成功空快照可以移除全部候选。
8. warming、内置策略和跨策略节点不得成为动态执行候选。

## 部署边界

本地发布工具属于仓库侧控制面，不进入稳航常驻进程。发布包先在生产目录同一文件系统
解压并验证，再把完整目标目录原子切换；LaunchAgent plist 单独原子替换。失败时恢复
切换前目录和 plist，并以同一套健康检查确认恢复结果。详细决策见
[ADR-0003](adr/0003-atomic-local-deployment.md)。
