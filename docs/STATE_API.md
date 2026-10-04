# 状态、状态机与只读 API 契约

本文是 Issue #4 的实现级契约；完整范围与验收清单保存在
`docs/archive/issue-drafts/004-versioned-state-api.md`。

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

v0.4.2 在每个代理组增加 `automation`：`performance_optimization_paused` 表示人工偏好期
只暂停性能回优，`safety_failover_active` 表示真实故障保护仍运行；同时提供
`manual_preference_remaining_seconds` 和 `manual_preference_remaining_text`。兼容机器码
继续使用 `manual_hold`，但不再表示停止故障切换。`safe_backup_available` 只根据现有缓存
中的成熟、未隔离候选计算；没有安全备援时文案必须明确 fail-closed，不能承诺一定切换。

持久状态继续使用 schema 2 的兼容扩展 `candidate_registry`。v0.3.0 会保留未知扩展字段，
因此 shadow 回滚不会破坏原状态；未来若出现不兼容结构变化再递增持久 schema。

v0.4.3（阶段 1）在 `service` 追加：`last_cycle_started_at`、`cycle_count`、
`cycle_duration_p50_ms`、`cycle_duration_p95_ms`（不足 5 轮为 `null`）、`stale_at`、
`stale_at_iso`、`stale_title`、`stale_detail`、`local_network_ok`；`updated_at`/`last_cycle_at`
语义改为周期完成时间，`next_cycle_at` 以周期开始时间加 20 秒计算，过期阈值为
`2 × 20 + 10 = 50` 秒。服务状态码新增 `local_network_offline`。每个代理组追加
`hot_standby`（节点 ID 或 `null`）、`business_targets_down`（仅数量，不暴露 URL）和
`metrics`：`failovers_24h`、`performance_switches_24h`、`last_failover_detect_seconds`。
legacy `service` 追加 `cycle_count`、`stale_at`、`stale_title`、`stale_detail`、
`last_probe_at`（完整周期或快速通道最近一次探测）、`fast_probe_interval_seconds`、
`timeline_window_seconds`；legacy 组追加 `timeline`、v1 组追加 `recent_probes`，元素为
`[unix 秒, 毫秒或 null, "probe"|"switch", 节点名]`，只保存在内存，最近 5 分钟、每组最多 90 点。看板只用
`stale_at` 与后端文案切换显示，不自行计算阈值。

v0.4.4 在 v1 与 legacy `service` 追加 `memory_current_mb`（当前实际占用；macOS 为
phys_footprint，与活动监视器"内存"列同口径；取不到时为 `null`）和 `memory_peak_mb`
（本进程生命周期峰值，与原 `memory_mb` 相同，`memory_mb` 保留兼容）。v1 另追加
`memory_trend_mb_per_hour`：每 10 分钟采样一次当前占用（持久状态 `memory_samples`，最多
144 个，进程重启即清空），满 2 小时后给出首尾各 6 个样本均值的每小时斜率，否则为 `null`。
持久状态新增 `cycle_count_merged`：首次运行时把 `cycle_count` 提升到节点最大采样数，
承接 v0.4.3 之前看板显示的累计轮数，只执行一次。两次完整周期之间，快速通道只更新
`generated_at`、`snapshot_id`、`uptime_seconds`、`last_probe_at`、内存字段与
`timeline`/`recent_probes`，其余字段以最近一次完整周期为准（最长 20 秒）。

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
状态过期。直接从仓库运行服务时，访问 `http://127.0.0.1:17654/acceptance` 可查看全部
fixture 及转换路径；该页面只读、同进程、本机回环。验收页和 fixture 不随安装器安装，
已安装的服务对这些地址返回 404。
每个样例同时标记为“真实运行快照可达”“同周期瞬时事件”或“契约边界模拟”，避免把
瞬时状态误解为运行中一定会停留的最终快照。

## 兼容与回退

持久状态只追加字段；不得只替换 Python 文件，也不得让旧版本覆盖未知 schema。需要退回时
用完整的旧版本重新安装（见 [DEPLOYMENT.md](DEPLOYMENT.md)），并保留本机诊断副本。

v0.4.5（看板重新设计）只追加字段，schema 仍为 2：

- `timeline` / `recent_probes` 元素扩展为 `[unix 秒, 毫秒或 null, "probe"|"standby"|"switch",
  节点名, 原因]`。`standby` 是热备的轻量探测（每轮一次，只用于曲线，不计入节点统计）；
  `switch` 的原因为 `failover`、`optimize` 或 `removed`，其余为 `null`。legacy `timeline`
  保留最近 30 分钟（看板可选 5 / 15 / 30 分钟），v1 `recent_probes` 仍为最近 5 分钟；
  每组最多 480 点，只在内存。
- legacy 组追加 `hot_standby`、`metrics` 和 `connections`：`{total, by_node, sites, more_sites,
  observed_at}`。`sites` 按站点聚合，每项 `{host, count, nodes, upload, download, since,
  process}`，最多 30 个，其余计入 `more_sites`。只给域名；没有域名的连接统一显示为
  “IP 直连（地址已隐藏）”，从不输出 IP、端口或完整 URL，也不写入日志和状态文件。
- legacy `service` 追加 `version`。`memory_peak_mb` 改为与 `memory_current_mb` 同口径的
  峰值（macOS 为 phys_footprint 峰值，Linux 为 VmHWM）；原 RSS 峰值仍在 `memory_mb`。

新增只读接口 `GET /api/nodes`，供“全部节点”页使用：列出 Clash 订阅中的每个节点（不含
策略组、内置出口和“到期 / 剩余流量”一类提示条目），每项 `{name, region, region_label,
type, udp, residential, group, role, delay_ms, delay_at}`。`role` 为 `current`、`standby`、
`monitored`（稳航选路范围内的同地区家宽）或 `view`（只供查看，永远不会被选中）；
`delay_ms` 取 Clash 自己最近一次测速（`0` 表示超时，`null` 表示未测）。顶层另有
`generated_at`、`total`、`residential`、`monitored`、`notices_hidden`、`truncated` 和
`regions`。目录在每次完整周期结束时生成并编码一次，最多 1000 个节点；请求只读缓存，
首个周期完成前返回 `503 catalog_unavailable`。接口不含服务器地址，也不影响选路。

新增静态页 `/nodes`、`/guide`、`/changelog` 和共享样式 `/assets/pages.css`，与看板同在
`127.0.0.1:17654`，只读取 `/api/status` 和 `/api/nodes`，不加载任何外部资源。

## 自动锁定（v0.5.0 起，v0.5.1 为唯一方式）

响应 schema 仍为 2，只追加字段：

- legacy `service.profile`：`auto_lock`。安装器以它和 `service.version` 判断新版本启动成功。
- legacy `service.auto_lock_idle`：已锁定但未接管切换的分组，`{分组名: {status, current,
  country, country_label, …}}`。`status` 为 `no_residential`（该国家没有家宽节点）、
  `unknown_country`（当前节点国家未识别）或 `paused`（分组当前选择的是另一个分组；
  `current_is_auto_group` 标出 url-test 等 Clash 自动分组）。
- legacy 组追加 `auto_lock`：`status`（`locked` / `relocked`）、`current`、`country`、
  `country_label`、`locked_at`、`previous`、`previous_label`、`candidates`（该国家家宽候选数）、
  `same_country_nodes`、`current_is_residential`。
- 组 `metrics` 追加 `adopt_switches_24h`（从同国家普通节点无损换到家宽的次数）。

持久状态中的锁定记录按（分组，国家）区分，改锁后旧记录清除。

## 设置接口（v0.5.1）

设置页 `/settings` 使用以下接口，与看板同在 `127.0.0.1:17654`。

只读：

- `GET /api/settings`：当前设置与 Clash 状态。包含 `profile`、`exclude_groups`、`ai_line`
  （`enabled`、`country`、`group_name`）、`managed_lines`、`manual`（手动添加的 AI 域名）、
  `applied`（最近一次写入 Clash 的时间、分组、被丢弃的可选规则、订阅 UID）、
  `applied_rule_count`、`rules_source`（ip.net.coffee 快照日期、最近同步 / 检查时间、错误、
  最近一次变化）、`geo`、`unsupported`（不能作为 AI 专线的地区）、`migration`（旧版迁移建议，
  没有时为 `null`）、`ai_rules_counts`，以及从控制器读取的 `countries`、`groups`、`ipv6`、
  `tun` 和 `clash`（能否定位当前订阅的扩展文件、是否找到内核）。控制器不可用时给出
  `controller_error`，其余字段照常返回。
- `GET /api/ai-check`：AI 分流体检。按 Clash 当前规则顺序，只读判断 34 条 net.coffee 条目
  各自命中的规则、分组链路、出口节点和国家，返回 `rows`、`total`、`ok`、`line_group`、
  `line_country`。

修改（POST）：

- `POST /api/settings/preview`：计算改动但不写任何文件。需要改 Clash 时返回每条线路的节点
  增减、将移除的旧分组、规则数量和前几条规则样例；关闭时返回将移除的分组、规则数和将恢复
  的原有分组数。
- `POST /api/settings/apply`：保存设置；需要时写入或撤销 Clash 中的专线，失败则全部回滚。
- `POST /api/settings/sync`：立即同步 net.coffee 规则（同样的安全检查），规则有变化时重新写入 Clash；返回 `{ok, changed, error, last_change}`。

请求体是 JSON 对象，只接受 `exclude_groups`、`ai_line`（`enabled`、`country`）、`manual`
和 `migration`（`accept` / `dismiss`）；其他键被忽略。POST 只在同时满足以下条件时
接受，否则返回 `403`：

- Host 为 `127.0.0.1:17654` 或 `localhost:17654`；
- `Origin` 为同源的 `http://127.0.0.1:17654` 或 `http://localhost:17654`；
- `Content-Type: application/json`；
- 请求头 `X-SteadyRoute: 1`。

请求体超过 64 KB 返回 `413`；输入无效返回 `400`；写入 Clash 失败返回 `409` 并附原因。
自定义请求头和 JSON 类型会触发浏览器 CORS 预检，而服务从不应答预检，因此其他网页无法
修改设置。每次成功修改记录一条 `settings_changed` 事件。
