# SteadyRoute 优化实施方案（面向 Codex 逐步执行）

分 5 个阶段执行，先做阶段 1：它同时解决“最近检测”显示延迟和故障切换慢这两个问题。每个任务都精确到文件、函数、测试和验收命令，可以逐个交给 Codex。

- 基线：`main` @ `082e8f6`（v0.4.2），Python 3.9 标准库、单进程、macOS LaunchAgent
- 当前测试：89 个，全部通过
- 适用范围：检测调度、故障切换、性能回优、隔离、休眠恢复、日志、内存指标、看板时效
- 不改变的约束：`mode=shadow`、fail-closed（绝不回落 DIRECT）、性能回优不断旧连接、真实故障才清理旧连接、人工偏好语义、看板只读缓存

---

## 0. 使用方式

1. 按阶段顺序执行：**阶段 0 → 1 → 2 → 3 → 4**。每个阶段一个功能分支、一个 PR、一次发布，上线观察达标后再开始下一阶段。
2. 每个阶段拆成若干任务（如 `1.3`）。**一次只交给 Codex 一个任务**，把第 4 节的提示词模板加上该任务全文一起发给它。
3. 每个任务都写明：目标、改哪些文件和函数、具体步骤、新增常量、必须新增的测试、验收命令、提交信息。Codex 完成一个任务后必须能通过 `./scripts/check.sh`。
4. 标注 **【你来执行】** 的步骤需要在你的 Mac 上跑（涉及生产目录或真实 Mihomo），Codex 不能也不应该代劳。

---

## 1. 代码审查结论（问题清单与证据）

行号对应 `src/steadyroute/weighted_router.py` @ `082e8f6`。

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| F1 | 周期是"跑完再睡 20 秒"，实际周期 = 耗时 D + 20 秒，会漂移 | `main()` 1460–1467：`run_cycle()` 后 `time.sleep(PROBE_INTERVAL_SECONDS)` | 检测频率低于设计值 |
| F2 | `updated_at` 记录的是周期**开始**时间，快照也按开始时间生成 | 1410 `state["updated_at"] = cycle_started_at`；1412 `now=cycle_started_at` | 看板"最近检测"最大能到 2D+20 秒；D≥20 秒时就会显示"1分钟前"。**这就是你看到的延迟，前端没有问题** |
| F3 | 业务探测失败后的重试在线程池之外**串行**执行 | 1351–1356、1369–1371，每次最长 8 秒 | 拉长 D |
| F4 | 切换前的业务预检**串行**执行：每个 URL 先测一次，失败再重测，单次超时 8 秒；故障切换时逐个候选重复 | `business_preflight()` 983–996；故障循环 1152–1162 | 台湾组 2 个 URL，每个候选最坏 32 秒，多个候选可能要几分钟，这段时间 AI 请求一直卡住 |
| F5 | 业务 URL 失败直接给当前节点 +2，没有区分是节点问题还是目标站点问题 | `update_effective_health()` 949–959 | claude.ai 或 chatgpt.com 自身故障时会误判节点故障，并对所有候选做 F4 那样的串行预检（**故障被放大**）；预检要求所有 URL 都成功，站点故障期间即使节点真坏了也切不走 |
| F6 | 故障确认靠"跨周期连续 2 次失败"，没有即时复测 | `FAILURES_BEFORE_SWITCH = 2`，每周期只测一次 | 从节点断开到切走通常 30～90 秒 |
| F7 | 备用节点轮询很稀疏（每组每轮只测 1 个），故障时按旧数据挑目标 | `STANDBY_PROBES_PER_GROUP = 1`；`choose_probe_targets()` 962–980 | 台湾 11 个候选，每个备用约 3～4 分钟才测一次；没有热备 |
| F8 | 分数里的可用率惩罚用**按样本**计算的 EWMA（α=0.2），乘以 4000 ms | 926–934；常量 64–68 | 当前节点偶发一次失败，分数立刻 +800 ms。**已用仓库代码模拟验证**：候选只比当前快 10 ms，当前节点失败 1 次后，恢复到第 6 次成功时就被性能切换切走 |
| F9 | 性能回优"3 轮确认"按周期计数，不要求候选有新样本 | 1229–1233；候选只在轮询到时才测 | 可能用同一份旧数据连续"确认" 3 次 |
| F10 | 切换门槛为 `≥180 ms 且 ≥30%` | 1214–1218 | 低延迟区间几乎永远不会切，线路劣化到 200 ms 以上才可能触发 |
| F11 | 24 小时可用率是硬窗口，而且 `availability_ewma ≥ 0.9` 也是准入条件 | `long_availability()` 867–873；`eligible_for_optimization()` 1043 | 旧故障要满 24 小时才能洗掉；一次失败会让备用节点被排除约 13 分钟（与采样频率有关） |
| F12 | 隔离期内每次失败都会把隔离**延长** 30 分钟；隔离期内的成功也计入恢复次数；没有退避 | `record_health_window()` 849–856 | 行为难预期；反复故障的节点和偶发故障的节点待遇一样 |
| F13 | 休眠恢复在探测**之后**才识别，而且用 `gap > 60s` 判断 | 1402–1409 | 唤醒后第一轮的失败已经计入统计，可能误判故障、误隔离；周期过慢时还会被误判成休眠 |
| F14 | 没有本地断网识别 | — | 断网时所有节点都失败，可能触发切换和误隔离 |
| F15 | "峰值内存"是 `ru_maxrss`，即进程生命周期内的**最高常驻内存（RSS）**，macOS 单位换算正确；没有当前值 | `memory_megabytes()` 214–218 | 看不出趋势，无法判断有没有泄漏 |
| F16 | 日志用 `print` 输出，由 launchd 重定向到 `router.log`，不轮换；每轮写 3 行左右（约 600 字节） | `log()` 97–99；plist `StandardOutPath` | 每天约 2～3 MB，持续增长（风险 R-04，Issue 003 未实施） |
| F17 | 看板"累计检测 N 轮"其实是节点样本数的最大值，不是周期数 | `dashboard.html` `renderNodes` 的 `sampleMax` | 文案不准确 |

---

## 2. 与之前讨论方案的差异

读完代码后，下面几处我改用了更合适的做法：

| 之前的建议 | 现在的方案 | 原因 |
|---|---|---|
| 门槛 `max(50 ms, 30%)` | **`max(100 ms, 30%)`**，另加每组每 24 小时最多 4 次性能切换 | 风险登记册 R-07：频繁换出口 IP 可能触发账号风控；50 ms 在 AI 流式输出中基本感知不到，不值得换 IP |
| 可用率改用 EWMA | **复用现有 24 个小时桶，按时间衰减加权（半衰期 6 小时）** | 不新增数据结构，改一个函数，历史数据可直接沿用 |
| 分数：延迟 + 抖动 | **p50 + 1.5 × (p90 − p50) + 1000 × (1 − 衰减可用率)** | 去掉按样本计算的高敏感惩罚（F8），同时保留一个稳定的可用率偏好 |
| 被动检测强信号直接判定故障 | **被动信号只触发立即复测，由主动复测给出结论**；先以 shadow 模式上线观察 | Mihomo 日志只能定位到代理组，错误文本也不稳定，直接判定误切风险太大 |
| 用本地直连判断断网 | **通过 Mihomo 的 `/proxies/DIRECT/delay` 探测**，只在失败时才测 | 复用现有控制器通道，不新增网络代码；只是测试，不会改变任何路由 |
| 日志 5 MB × 3 | **沿用仓库 Issue 003：主日志 5 MiB × 5，错误日志 1 MiB × 3** | 与已有需求保持一致 |
| 前端改 SSE | **不改**。前端已经每 5 秒拉取、每秒本地刷新 | 延迟根因在后端（F1/F2），修后端即可 |

---

## 3. 阶段总览

| 阶段 | 目标 | 分支 | 建议版本 | 优先级 | 上线后验收指标 |
|---|---|---|---|---|---|
| 0 | 采集基线、补充运行指标 | `chore/runtime-metrics` | 随阶段 1 发布 | P0 | 有周期耗时 p50/p95、切换计数和检测耗时数据 |
| 1 | 检测时效和故障快速切换 | `fix/fast-failover` | v0.4.3 | P0 | 周期耗时 p95 < 6 秒；"最近检测"始终 ≤ 30 秒；节点断开后 ≤ 30 秒切走；站点故障不误切；唤醒和断网不误判 |
| 2 | 日志轮换与内存指标（Issue 003） | `fix/bounded-logging` | v0.4.4 | P1 | 日志总量 ≤ 34 MiB；看板显示当前和峰值内存 |
| 3 | 选路策略 v2 | `feat/routing-policy-v2` | v0.5.0（见下注） | P1 | 无"偶发失败诱发切换"；劣化 5 分钟内切走；每组每天性能切换 ≤ 4 次 |
| 4 | 被动检测（先 shadow，再 active） | `feat/passive-detection` | v0.5.1 / v0.5.2 | P2 | 有流量时 ≤ 15 秒切走；shadow 期精确率 ≥ 80% 才启用 |

> 注：CHANGELOG 里 v0.5.0 预留给"动态候选正式接管"。如果仍按这个顺序，阶段 3 和 4 可改为 v0.6.x，或者把动态接管顺延，由管理窗口决定。

依赖关系：阶段 1 依赖 0；阶段 3 依赖 1（用到热备和样本新鲜度）；阶段 4 依赖 1（复用即时复测）和 2（日志限频）。

---

## 4. 通用执行规则（每个任务都适用）

### 4.1 给 Codex 的提示词模板

```text
仓库：~/Projects/steadyroute，分支：<分支名>。
先阅读 README.md、CONTEXT.md、docs/ARCHITECTURE.md、docs/STATE_API.md、docs/TESTING.md，
以及 docs/plans/OPTIMIZATION_PLAN.md 的第 4 节和任务 <编号>。
只完成任务 <编号>，不要提前实现后续任务。
约束：Python 3.9 标准库；不修改生产目录；不重启服务；不部署；route-policies.json 保持 mode=shadow。
先写失败的测试，再实现；完成后运行 ./scripts/check.sh 并全部通过。
按任务要求更新 CHANGELOG.md 的 [Unreleased] 和相关文档。
最后汇报：修改的文件、新增测试名称、check.sh 结果、未解决的风险。
```

建议先把本文件原样放进仓库 `docs/plans/OPTIMIZATION_PLAN.md`，并单独提交一次文档 PR，这样 Codex 能直接读取。

### 4.2 代码约束

- 只用 Python 3.9 可用的语法和 API：不用 `match`、`X | Y` 类型注解、`zip(strict=...)` 等 3.10 及以上才有的特性。
- 新增的**纯计算逻辑**放到新模块 `src/steadyroute/health_model.py`（阶段 1 创建），不做 I/O、不读时钟，时间全部由参数传入，方便测试。
- 所有新常量放在 `weighted_router.py` 顶部常量区，并在 `build_status_snapshots()` 的 `policies`（v1）和 `policy`（legacy）里暴露需要展示的项。
- 状态文件只做**向后兼容的字段追加**，`STATE_SCHEMA_VERSION` 保持 2。读取新字段一律用 `.get(key, 默认值)`。
- API 只追加字段，`API_SCHEMA_VERSION` 保持 2。如果现有契约测试或 `fixtures/status_contract_v2.json` 因新增字段失败，同步更新 fixture 和 `docs/STATE_API.md`。
- 任何代码路径都不能对 `DIRECT`、`COMPATIBLE`、`REJECT` 执行 `PUT /proxies/...`。本方案只允许对 `DIRECT` 调用 `GET /proxies/DIRECT/delay` 做本地网络检测。

### 4.3 新增模块接入清单（每新增一个 `.py` 都要做）

以新增 `health_model.py` 为例：

1. `scripts/check.sh`：增加 `python3 -m py_compile "$PROJECT_DIR/src/steadyroute/health_model.py"`。
2. `scripts/build-release.sh`：增加 `cp "$PROJECT_DIR/src/steadyroute/health_model.py" "$PACKAGE_DIR/src/"`。
3. `scripts/deploy.py`：文件映射表（约 37–43 行）增加 `"src/health_model.py": "health_model.py"`；两处 `py_compile` 列表（约 174–177 行、530–531 行）各加一行。
4. `tests/test_deploy.py`：构造发布包的夹具（约 129–137 行）增加该文件。
5. `weighted_router.py` 顶部的 `try: import ... except ModuleNotFoundError:` 两个分支都加上 `import health_model`。
6. 运行 `./scripts/check.sh`。

### 4.4 每个 PR 的完成定义

- [ ] `./scripts/check.sh` 通过
- [ ] CHANGELOG `[Unreleased]` 已更新（Added / Changed / Fixed / Safety）
- [ ] `docs/ARCHITECTURE.md`、`docs/STATE_API.md`、`docs/OPERATIONS.md`、`docs/TESTING.md` 中受影响的部分已更新
- [ ] 运行 `python3 scripts/benchmark-status.py <旧版本源码目录>` 和 `python3 scripts/benchmark-status.py .`，把 RSS 与接口耗时写入 `docs/PERFORMANCE_<版本>.md`；RSS 增量 ≤ 1 MB
- [ ] PR 描述写明：改动、未改动、测试证据、生产风险、回滚方法

---

## 5. 阶段 0：基线与运行指标

目的：改之前先量化，改之后用同一把尺子对比。

### 任务 0.1 【你来执行】采集生产基线

在 Mac 上运行，全部只读：

```bash
# 1) 周期耗时与快照年龄，每 5 秒采样一次，共 10 分钟
for i in $(seq 1 120); do
  curl --noproxy '*' -s http://127.0.0.1:17654/api/v1/status | python3 -c '
import json,sys,time
s=json.load(sys.stdin)["service"]
print(int(time.time()), s["last_cycle_duration_ms"], int(time.time())-s["last_cycle_at"])'
  sleep 5
done > ~/steadyroute-baseline.txt
sort -n -k2 ~/steadyroute-baseline.txt | tail -5   # 周期耗时最大的 5 次（毫秒）
sort -n -k3 ~/steadyroute-baseline.txt | tail -5   # 看板时间差最大的 5 次（秒）

# 2) 日志体积与增长
LOG=~/Library/Logs/Clash-Verge-Stability-Router
ls -lh "$LOG"; wc -l "$LOG"/router.log
head -1 "$LOG"/router.log | cut -c1-21; tail -1 "$LOG"/router.log | cut -c1-21

# 3) 切换与休眠事件次数
grep -c "FAILOVER" "$LOG"/router.log
grep -c "OPTIMIZE" "$LOG"/router.log
grep -c "resume detected" "$LOG"/router.log
grep -c "failed business preflight\|business preflight failed" "$LOG"/router.log
```

把结果保存下来，阶段 1 上线后用同样的命令对比。预期：第 1 项的周期耗时经常在 5～25 秒，时间差会出现 40～70 秒。

### 任务 0.2 周期与切换指标（Codex）

**目标**：状态和 API 里有足够的数据判断优化效果。

**修改**：`weighted_router.py`

1. 在 `run_cycle()` 末尾（写 `last_cycle_duration_ms` 的位置）追加：
   - `state["cycle_count"] = int(state.get("cycle_count", 0)) + 1`
   - `state["cycle_durations_ms"]`：列表，追加本轮耗时，只保留最后 60 个。
2. `evaluate_group()`：
   - 故障切换成功时（1167 行 `select_node` 之后）：向 `group_state["failover_times"]` 追加 `int(now)`，只保留最近 10 个。
   - 性能回优成功时（1256 行 `select_node` 之后）：向 `group_state["performance_switch_times"]` 追加 `int(now)`，只保留最近 10 个。
   - 故障切换时记录检测耗时：`group_state["last_failover_detect_seconds"] = int(now) - int(current_stats.get("first_failure_at", now))`。
3. `update_node_stats()`：失败且 `failure_streak` 从 0 变 1 时写 `node_state["first_failure_at"] = int(observed_at)`；成功时删除该字段。
4. `build_status_snapshots()`：
   - v1 `service` 增加 `cycle_count`、`cycle_duration_p50_ms`、`cycle_duration_p95_ms`（样本不足 5 个时为 `null`，用最近邻秩百分位）。
   - v1 每个 group 增加 `metrics`：`failovers_24h`、`performance_switches_24h`（统计 `now - t < 86400` 的条目数）、`last_failover_detect_seconds`（没有时为 `null`）。
   - legacy `service` 增加 `cycle_count`。

**测试**（新建 `tests/test_runtime_metrics.py`）：
- `test_cycle_durations_are_bounded_to_60`：连续写入 70 次，长度为 60，保留最新值。
- `test_percentiles_null_when_fewer_than_5_samples`
- `test_switch_counters_only_count_last_24h`：构造 `now-90000` 和 `now-100` 两条，`failovers_24h == 1`。
- `test_failover_records_detection_seconds`：当前节点 `first_failure_at = now-37`，触发故障切换后 `last_failover_detect_seconds == 37`（复用 `test_weighted_router.py` 里替换 `select_node` 和 `close_old_connections` 的写法）。

**验收**：`./scripts/check.sh` 通过。

**提交**：`feat(metrics): record cycle duration and switch counters`

---

## 6. 阶段 1：检测时效与故障快速切换（P0）

**分支**：`fix/fast-failover`，**版本**：v0.4.3

**阶段目标**：
- 周期严格按 20 秒排期，正常周期耗时 < 3 秒，p95 < 6 秒；
- 看板"最近检测"始终 ≤ 30 秒，超过 50 秒明确显示"检测延迟"；
- 节点断开后 ≤ 30 秒完成切换（阶段 4 上线后，有流量时 ≤ 15 秒）；
- 目标站点故障、本地断网、Mac 唤醒都不会误切、误隔离。

### 任务 1.1 新建 `health_model.py`（纯函数）

**文件**：新建 `src/steadyroute/health_model.py`，按 4.3 节接入。

**内容**（阶段 1 只需要以下函数，阶段 3 再追加）：

```python
"""Pure health and scheduling math for SteadyRoute. No I/O, no clocks."""

import math


def percentile(values, fraction):
    """Nearest-rank percentile. values must be non-empty."""
    ordered = sorted(float(v) for v in values)
    rank = max(1, int(math.ceil(float(fraction) * len(ordered))))
    return ordered[min(len(ordered), rank) - 1]


def next_deadline(previous_deadline, interval, mono_now):
    """Fixed-rate schedule that never bursts to catch up after overrun or sleep."""
    deadline = float(previous_deadline) + float(interval)
    if deadline <= float(mono_now):
        return float(mono_now)
    return deadline


def detect_resume(prev_wall, prev_mono, wall_now, mono_now, interval,
                  last_duration_seconds, slack_seconds=15.0):
    """Return (slept, wall_gap_seconds).

    On macOS time.monotonic() does not advance during system sleep, so a
    wall/monotonic divergence identifies sleep precisely. The gap rule is a
    fallback for platforms whose monotonic clock does advance during sleep.
    """
    if prev_wall is None or prev_mono is None:
        return False, 0
    wall_gap = float(wall_now) - float(prev_wall)
    mono_gap = float(mono_now) - float(prev_mono)
    slept = (wall_gap - mono_gap) > float(slack_seconds) or \
        wall_gap > float(interval) * 3 + float(last_duration_seconds)
    return slept, int(max(0.0, wall_gap))
```

**测试**（新建 `tests/test_health_model.py`）：
- `test_percentile_nearest_rank`：`[10,20,30,40]` 的 0.5 为 20，0.9 为 40；单元素返回自身。
- `test_next_deadline_is_fixed_rate`：`next_deadline(100, 20, 105) == 120`。
- `test_next_deadline_never_bursts`：`next_deadline(100, 20, 500) == 500`。
- `test_detect_resume_by_clock_divergence`：wall 前进 600、mono 前进 20 → `(True, 600)`。
- `test_detect_resume_ignores_normal_cycle`：wall 与 mono 都前进 21 → `(False, 21)`。
- `test_detect_resume_gap_fallback`：wall、mono 都前进 200，interval 20，耗时 5 → `True`。
- `test_detect_resume_first_cycle`：`prev_wall=None` → `(False, 0)`。

**提交**：`feat(health): add pure scheduling and percentile helpers`

### 任务 1.2 固定速率调度，修正时间戳语义（修复 F1、F2、F13 的一部分）

**修改**：`weighted_router.py`

1. 新增模块级运行时字典（只在内存中，不持久化）：
   ```python
   RUNTIME = {"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0}
   ```
2. 重写 `main()` 的循环：
   ```python
   deadline = time.monotonic()
   while True:
       try:
           run_cycle(dry_run=args.dry_run)
       except Exception as error:
           log("cycle failed: %s" % error)
       if not args.daemon:
           break
       deadline = health_model.next_deadline(deadline, PROBE_INTERVAL_SECONDS, time.monotonic())
       WAKE_EVENT.wait(max(0.0, deadline - time.monotonic()))
       WAKE_EVENT.clear()
   ```
   新增模块级 `WAKE_EVENT = threading.Event()`（阶段 4 的被动检测用它唤醒主循环，本阶段只是占位）。
3. `run_cycle()` 开头（`load_state()` 之后、探测之前）做休眠识别：
   ```python
   wall_now, mono_now = time.time(), time.monotonic()
   slept, gap = health_model.detect_resume(
       RUNTIME["last_cycle_wall"], RUNTIME["last_cycle_mono"], wall_now, mono_now,
       PROBE_INTERVAL_SECONDS, RUNTIME["last_duration_s"])
   if RUNTIME["last_cycle_wall"] is None:          # 进程刚启动：沿用磁盘上的 updated_at 判断
       previous = int(state.get("updated_at", 0))
       slept = bool(previous) and cycle_started_at - previous > PROBE_INTERVAL_SECONDS * 3
       gap = max(0, cycle_started_at - previous) if previous else 0
   RUNTIME["last_cycle_wall"], RUNTIME["last_cycle_mono"] = wall_now, mono_now
   if slept:
       state["last_resume_at"] = cycle_started_at
       state["last_sleep_gap_seconds"] = gap
       log("resume detected after %d seconds without probes" % gap)
   resume_cycle = slept
   ```
   删除 `run_cycle()` 末尾原来的 1402–1409 行（事后识别）。保留 `last_cycle_gap_seconds` 的计算，但改为在开头计算。
4. `run_cycle()` 末尾：
   ```python
   completed_at = int(time.time())
   state["last_cycle_started_at"] = cycle_started_at
   state["updated_at"] = completed_at                      # 语义改为"周期完成时间"
   duration = time.perf_counter() - cycle_clock
   state["last_cycle_duration_ms"] = int(round(duration * 1000))
   RUNTIME["last_duration_s"] = duration
   snapshots = build_status_snapshots(state, proxy_data, connections, now=completed_at)
   ```
   控制器失败的分支（1279–1285）同样用 `completed_at`。
5. `build_status_snapshots()`：
   - 新常量 `CYCLE_BUDGET_SECONDS = 10`；`stale_after = PROBE_INTERVAL_SECONDS * 2 + CYCLE_BUDGET_SECONDS`（50 秒，原来是 60 秒）。
   - v1 `service` 增加：`last_cycle_started_at`、`stale_at`（`last_cycle + stale_after`，没有周期时为 `null`）、`stale_title`（固定为"检测延迟"）、`stale_detail`（"超过 50 秒没有完成检测周期，页面数据可能已过时。"，秒数取 `stale_after`）。
   - `next_cycle_at` 改为 `last_cycle_started_at + PROBE_INTERVAL_SECONDS`。
   - legacy `service` 增加 `stale_at`、`stale_title`、`stale_detail`。

**测试**（`tests/test_status_api.py` 追加；新建 `tests/test_scheduler.py`）：
- `test_updated_at_is_cycle_completion_time`：mock `time.time` 依次返回 1000（开始）和 1007（完成），跑 `run_cycle`（参照 `tests/test_dynamic_cycle.py` 的 mock 方式），断言 `state["updated_at"] == 1007`、`state["last_cycle_started_at"] == 1000`。
- `test_resume_detected_before_probes`：让 `probe_url` 的 mock 在被调用时断言 `state["last_resume_at"]` 已经写入。
- `test_stale_at_exposed_in_both_apis`：`stale_at == updated_at + 50`，v1 和 legacy 一致。
- `test_state_staleness_threshold_is_two_intervals_plus_budget`：`now - updated_at = 51` 时 `state_stale` 为真，49 时为假。
- 现有 `test_state_staleness_is_calculated_by_backend` 应仍然通过（61 > 50）。

**验收**：`./scripts/check.sh` 通过；本地 `python3 src/steadyroute/weighted_router.py --once --dry-run` 不报错（没有 Mihomo 时会打印 cycle failed，属于正常）。

**提交**：`fix(scheduler): fixed-rate cycles and completion-time freshness`

### 任务 1.3 探测全部并行、限定耗时（修复 F3、F4 的串行部分）

**修改**：`weighted_router.py`

1. 常量调整：
   - `PROBE_TIMEOUT_MS = 3000`（原 5000）
   - `BUSINESS_PROBE_TIMEOUT_MS = 5000`（原 8000）
   - 线程池 `max_workers=6` 改为 `PROBE_WORKERS = 10`
2. 新增辅助函数，所有"同时测多个"的地方都用它：
   ```python
   def probe_many(jobs, timeout_ms):
       """jobs: list of (key, node_name, url). Returns {key: delay_or_None}. Runs in parallel."""
       if not jobs:
           return {}
       results = {}
       with concurrent.futures.ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(jobs))) as executor:
           futures = {executor.submit(probe_url, name, url, timeout_ms): key for key, name, url in jobs}
           for future in concurrent.futures.as_completed(futures):
               results[futures[future]] = future.result()
       return results
   ```
3. `run_cycle()` 中两段串行重试（1351–1356 当前节点、1365–1372 其他节点）合并为一次 `probe_many`：先收集所有 `delay is None` 的业务结果，统一并行重试一次，再分别写回。
4. 重写 `business_preflight()`：
   ```python
   def business_preflight(group_name, node_name, state, dry_run=False, now=None):
       now = time.time() if now is None else now
       urls = BUSINESS_TEST_URLS.get(group_name, [])
       if dry_run or not urls:
           return True
       first = probe_many([(url, node_name, url) for url in urls], BUSINESS_PROBE_TIMEOUT_MS)
       failed = [url for url in urls if first.get(url) is None]
       retry = probe_many([(url, node_name, url) for url in failed], BUSINESS_PROBE_TIMEOUT_MS)
       success = all(first.get(url) is not None or retry.get(url) is not None for url in urls)
       record_business_result(state["nodes"].setdefault(node_name, {}), success, now)
       return success
   ```
   （任务 1.5 会再加入"新鲜结果跳过"和"目标站点故障排除"两条规则。）

**测试**（`tests/test_fast_failover.py` 新建）：
- `test_preflight_probes_urls_in_parallel`：`probe_url` mock 里 `time.sleep(0.3)` 后返回 100；2 个 URL 的预检总耗时 < 0.5 秒。
- `test_preflight_retries_only_failed_urls_once`：第一个 URL 首次失败、重试成功，第二个一次成功 → 返回 True，`probe_url` 共调用 3 次。
- `test_business_retries_are_parallel_in_cycle`：两组当前节点业务探测都首次失败，`run_cycle` 中重试阶段耗时 < 单次 sleep 的 1.5 倍。

**提交**：`perf(probe): parallelize business retries and preflight`

### 任务 1.4 热备节点（修复 F7）

**修改**：`weighted_router.py`

1. 新增：
   ```python
   def select_hot_standby(candidates, nodes, current):
       return best_failover(candidates, nodes, current)
   ```
2. `choose_probe_targets()`：每组在确定 `current` 后：
   ```python
   hot = select_hot_standby(candidates, state["nodes"], current)
   group_state["hot_standby"] = hot
   if hot:
       targets.add(hot)
   ```
   轮询游标逻辑保持不变，但**跳过** `hot` 和 `quarantine_until > now` 的节点（隔离期内的节点不再占用轮询名额，见任务 3.5 的半开放探测）。
3. `run_cycle()` 的业务探测调度（1303–1321）：给当前节点安排业务探测时，如果存在 `hot_standby`，**用同一个 URL** 同时给热备安排一次业务探测，`future_meta` 标记为 `("business_standby", group_name, hot, url)`。热备的结果调用 `record_business_result()` 记录（它既用于任务 1.5 的差分判断，也让热备的业务结果保持新鲜）。
4. v1 每个 group 增加 `hot_standby`：节点 id（没有时为 `null`）。

**测试**（`tests/test_fast_failover.py`）：
- `test_hot_standby_is_probed_every_cycle`：连续 3 次 `choose_probe_targets`，热备每次都在目标里。
- `test_hot_standby_excludes_quarantined_and_immature`
- `test_round_robin_skips_hot_standby_and_quarantined`
- `test_standby_business_probe_uses_same_url_as_current`

**提交**：`feat(probe): keep a hot standby per group`

### 任务 1.5 业务故障差分判断（修复 F5）

**规则**：当前节点业务 URL 失败（含重试）时：

| 热备同一 URL | 判定 | 处理 |
|---|---|---|
| 成功 | 节点侧故障 | 与现在相同：`effective_failure_streak += 2` |
| 失败 | 目标站点故障 | 不计入节点故障；记录 `group_state["business_target_down"][url] = now + 300` |
| 没有热备 | 无法区分 | 保守处理：`effective_failure_streak += 1` |

当前节点该 URL 成功时，删除对应的 `business_target_down` 条目。

当前节点首次失败时，任务 1.3 的并行重试批次里**同时重试热备**（如果热备首次也失败），避免热备的一次偶发失败把节点故障误判成站点故障。

**修改**：

1. `update_effective_health()` 增加参数 `business_verdict`，取值 `"ok" | "node" | "target" | "unknown" | None`：
   - `"ok"` → 置 0；`"node"` → +2；`"target"` → 按基础探测结果处理（基础成功置 0，失败 +1）；`"unknown"` → +1；`None`（本轮没测业务）→ 保持原逻辑。
2. `run_cycle()` 汇总业务结果时，根据上表计算 `business_verdict`。
3. `business_preflight()` 加两条规则：
   - 过滤掉 `business_target_down[url] > now` 的 URL；过滤后为空则直接返回 True（此时仍要求节点满足 `eligible_for_optimization`，也就是基础探测健康）。
   - **新鲜跳过**：如果节点 `business_last_success` 为真、`now - business_checked_at <= PREFLIGHT_FRESH_SECONDS`（新常量，120）、且 `last_success` 为真、`now - last_probe_at <= PROBE_INTERVAL_SECONDS + CYCLE_BUDGET_SECONDS`，直接返回 True，并记录 `node_state["preflight_skipped_at"] = int(now)`。增加参数 `allow_fresh_skip=True`（任务 1.7 使用）。
4. 事件：目标站点首次被判定故障时，用 `state_contract` 相同格式追加事件 `BUSINESS_TARGET_UNREACHABLE`（severity `warning`，scope `group`，`reason_code` 为 `target_side_failure`）。同一 URL 在 `business_target_down` 未过期期间不重复记录。**事件中不要写入 URL**（遵守 API 白名单），只写组 id。
5. v1 每个 group 增加 `business_targets_down`：数量（整数），不暴露 URL。

**测试**（`tests/test_fast_failover.py`）：
- `test_target_side_failure_does_not_count_against_node`：当前和热备都失败 → `effective_failure_streak` 不增加，不调用 `select_node`。
- `test_node_side_business_failure_triggers_failover`：当前失败、热备成功 → `effective_failure_streak >= 2`，本轮切到热备。
- `test_preflight_ignores_target_down_urls`：claude URL 标记为故障，只测 chatgpt URL。
- `test_preflight_fresh_skip`：热备 60 秒前业务成功、本轮基础成功 → 不调用 `probe_url`。
- `test_target_down_event_is_deduplicated`：连续 3 轮目标站点故障只产生 1 条事件，事件字段里没有 `http`。

**提交**：`fix(health): distinguish target outages from node failures`

### 任务 1.6 即时复测与本地断网识别（修复 F6、F14）

**新常量**：
```python
CONFIRM_TIMEOUT_MS = 3000
CONFIRM_PROBES = 2                    # 首次失败后再连测 2 次，3 次全失败才确认
LOCAL_CHECK_TIMEOUT_MS = 3000
LOCAL_CHECK_URLS = (
    "http://captive.apple.com/hotspot-detect.html",
    "https://www.baidu.com/favicon.ico",
)
```

**新增函数**：

```python
def local_network_ok():
    """Probe DIRECT through Mihomo's delay API. Read-only: never selects DIRECT."""
    results = probe_many(
        [(url, "DIRECT", url) for url in LOCAL_CHECK_URLS], LOCAL_CHECK_TIMEOUT_MS)
    return any(value is not None for value in results.values())


def confirm_current_failure(current, cycle_cache):
    """Return 'confirmed' | 'transient' | 'local_offline'.

    cycle_cache is a dict shared within one cycle so the local check runs at most once.
    """
    need_local = "local_ok" not in cycle_cache
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(probe_url, current, TEST_URL, CONFIRM_TIMEOUT_MS)
        local = executor.submit(local_network_ok) if need_local else None
        first_delay = first.result()
        if local is not None:
            cycle_cache["local_ok"] = local.result()
    if not cycle_cache["local_ok"]:
        return "local_offline"
    if first_delay is not None:
        return "transient"
    for _ in range(CONFIRM_PROBES - 1):
        if probe_url(current, TEST_URL, CONFIRM_TIMEOUT_MS) is not None:
            return "transient"
    return "confirmed"
```

**`run_cycle()` 的处理顺序**（在"写入节点统计"之前插入）：

1. 所有探测结果收齐后，对每组：如果当前节点**基础探测**失败，调用 `confirm_current_failure(current, cycle_cache)`，结果写入 `group_state["last_confirm"] = {"verdict": ..., "at": cycle_started_at}`。业务判定为 `"node"` 的情况不再复测：热备用同一 URL 已经成功，说明本地网络正常、问题在当前节点，按任务 1.5 直接 +2 进入故障切换。
2. 如果**任一**组返回 `local_offline`：
   - 本轮**只记录成功样本**，失败样本全部丢弃（不写 `short_results`、`long_buckets`、`recent_failures`，不增加 `failure_streak`）；`update_effective_health()` 只处理成功（成功置 0），失败时保持原值；
   - `state["local_network_ok"] = False`；
   - **跳过本轮所有 `evaluate_group()`**（不切换、不隔离）；
   - 追加事件 `LOCAL_NETWORK_OFFLINE`（去重：从 True 变 False 时记录一次；恢复时记录 `LOCAL_NETWORK_RECOVERED`）；
   - `WAKE_EVENT` 不需要处理，下一轮按正常 20 秒排期。
3. 否则 `state["local_network_ok"] = True`，然后：
   - `confirmed` → 当前节点 `effective_failure_streak = max(原值, FAILURES_BEFORE_SWITCH)`，本轮 `evaluate_group()` 直接进入故障切换；
   - `transient` → 按原逻辑只记一次失败。
4. **复测结果不写入节点统计窗口**，只影响本轮判定，避免多出来的样本扭曲可用率。
5. 实现方式：把"写入统计"那段循环改成接收一个 `record_failures`（布尔）参数的函数 `record_probe_results(state, targets, base_results, cycle_started_at, record_failures)`。

**唤醒周期**（与任务 1.2 的 `resume_cycle` 联动）：`resume_cycle` 为真时，本轮同样**只记录成功样本**、跳过 `evaluate_group()`；如果本地检测失败，则设置 `state["local_network_ok"] = False`。唤醒后的第二轮（20 秒后）恢复正常判定。原因：Mac 唤醒后 QUIC/hy2 会话都要重建，第一轮的失败不代表节点故障。

**看板服务状态**：`build_status_snapshots()` 的 `service_state` 在 `stale` 判断之后、`resumed` 之前增加：

```python
elif state.get("local_network_ok") is False:
    service_state = {
        "code": "local_network_offline", "severity": "warning", "title": "本机网络不可用",
        "detail": "直连检测失败，已暂停故障判定和切换，避免误伤节点。",
        "next_action": "网络恢复后自动继续检测。",
    }
```

如果 fixture 或契约测试枚举了服务状态码，把 `local_network_offline` 加进去，并在 `docs/STATE_API.md` 登记。

**测试**（`tests/test_fast_failover.py`）：
- `test_confirmed_failure_switches_in_same_cycle`：当前节点基础探测和两次复测都失败、本地检测成功、热备健康 → 同一次 `run_cycle` 内调用 `select_node(group, 热备)` 和 `close_old_connections`。
- `test_transient_failure_does_not_switch`：首次失败、第一次复测成功 → 不切换，`failure_streak == 1`。
- `test_local_offline_freezes_decisions`：本地检测失败 → 不调用 `select_node`，所有节点的 `short_results` 长度不变，`state["local_network_ok"] is False`。
- `test_local_check_runs_once_per_cycle`：两组同时失败，`DIRECT` 的探测只发生 `len(LOCAL_CHECK_URLS)` 次。
- `test_local_check_never_selects_direct`：整个流程中 `select_node` 从未收到 `DIRECT`，`api_request` 从未收到 `PUT /proxies/DIRECT`。
- `test_resume_cycle_records_only_successes`：`resume_cycle` 为真时失败不入窗口、不切换。
- `test_confirm_probes_do_not_inflate_samples`：确认流程后 `samples` 只增加 1。

**提交**：`feat(failover): confirm failures in-cycle and guard local outages`

### 任务 1.7 故障风暴保护

**规则**：同一组 10 分钟内故障切换 ≥ 3 次时：
- 关闭预检"新鲜跳过"，每次故障切换都必须完整预检；
- 故障切换前必须 `local_network_ok()` 为真；
- 追加事件 `FAILOVER_STORM`（severity `warning`，10 分钟内只记一次）；
- **仍然允许故障切换**（当前节点确实坏了，暂停切换只会更糟）。

**修改**：`evaluate_group()` 故障分支开头读取 `group_state["failover_times"]`（任务 0.2 已记录），计算 `storm = len([t for t in times if now - t < 600]) >= 3`，把 `storm` 传给 `business_preflight(..., allow_fresh_skip=not storm)`。

**测试**：`test_failover_storm_requires_full_preflight`、`test_failover_storm_event_once_per_10_minutes`。

**提交**：`feat(failover): add failover storm guard`

### 任务 1.8 看板时效显示

**修改**：`src/steadyroute/dashboard.html`

1. `age` 函数改为 120 秒以内按秒显示：
   ```js
   const age=s=>s<2?'刚刚':s<120?`${s}秒前`:`${Math.floor(s/60)}分钟前`;
   ```
2. `render()` 中根据后端给的 `stale_at` 切换文案（阈值和文案都来自后端，前端不自己算状态）：
   ```js
   const stale=d.service.stale_at!=null&&now>=d.service.stale_at;
   const title=stale?d.service.stale_title:d.service.state_title;
   const severity=stale?'warning':d.service.state_severity;
   ```
   用 `title`、`severity` 替换原来的 `state_title`、`state_severity`。
3. 页面切回前台时立即拉取：
   ```js
   document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
   ```
4. "累计检测 N 轮"改用 `d.service.cycle_count`（任务 0.2 已提供），没有时回退到原来的 `sampleMax`。
5. 顶部文案改为 `最近检测 ${age(a)}`，内存部分留给任务 2.4 修改。

**测试**（`tests/test_status_api.py` 追加）：
- `test_dashboard_uses_backend_stale_deadline`：源码包含 `stale_at`、`stale_title`、`visibilitychange`；仍然**不包含** `a>d.service.probe_interval_seconds`（现有断言保留）。
- `test_dashboard_counts_cycles_not_samples`：源码包含 `cycle_count`。

**人工验收**：参照 `scripts/benchmark-status.py` 写一个临时脚本（不提交）：构造 `updated_at = now - 55` 的状态，调用 `update_dashboard_cache()`，把 `router.DASHBOARD_PATH` 指向仓库内的 `src/steadyroute/dashboard.html`，启动 `DashboardHandler` 并用浏览器打开，页面应显示"检测延迟"。

**提交**：`fix(dashboard): second-level freshness and backend stale deadline`

### 任务 1.9 阶段收尾

1. 更新 `CHANGELOG.md` `[Unreleased]`：
   - Fixed：固定速率周期；`updated_at` 改为周期完成时间；业务重试和预检并行；业务失败区分节点与站点；唤醒首轮不计失败。
   - Added：热备节点、即时复测、本地断网保护、故障风暴保护、周期与切换指标、`stale_at`。
   - Safety：`DIRECT` 只用于只读延迟探测；shadow 保持不变。
2. 更新 `docs/STATE_API.md`（新增字段表）、`docs/ARCHITECTURE.md`（探测调度一节）、`docs/OPERATIONS.md`（新服务状态 `local_network_offline` 与事件 `BUSINESS_TARGET_UNREACHABLE`、`FAILOVER_STORM` 的处置方式）、`docs/TESTING.md`。
3. `docs/RISK_REGISTER.md` 新增：R-17"目标站点故障被误判为节点故障"（已缓解）、R-18"本地断网误隔离"（已缓解）。
4. 写 `docs/PERFORMANCE_0.4.3.md`。
5. 版本号：`VERSION` 和 `pyproject.toml` 改为 `0.4.3`。

**上线后验收【你来执行】**（部署按 `docs/DEPLOYMENT.md` 的标准流程）：
- 重跑任务 0.1 第 1 项：周期耗时 p95 < 6000 ms，时间差最大值 ≤ 30 秒；
- 在 Clash Verge 里临时把当前台湾节点的服务器端口改错（或断开该节点），记录从断开到看板显示切换的时间，目标 ≤ 30 秒；
- 合盖 5 分钟再打开，事件里没有 `failover_now`，没有新增隔离；
- 关闭 Wi‑Fi 30 秒再打开，服务状态出现"本机网络不可用"，恢复后自动回到"运行中"，没有切换和隔离。

**回滚**：`./scripts/rollback-local.sh` 预演后 `--apply`，回到 v0.4.2 完整包。状态文件只追加了字段，v0.4.2 可以直接读取。

---

## 7. 阶段 2：日志轮换与内存指标（P1，实施 Issue 003）

**分支**：`fix/bounded-logging`，**版本**：v0.4.4

### 任务 2.1 应用内日志轮换

**新文件**：`src/steadyroute/logging_setup.py`（按 4.3 节接入）

```python
"""Bounded application logging for SteadyRoute (stdlib only)."""

import logging
import logging.handlers
import os
import sys
import threading

MAIN_LOG_BYTES = 5 * 1024 * 1024
MAIN_LOG_BACKUPS = 5
ERROR_LOG_BYTES = 1 * 1024 * 1024
ERROR_LOG_BACKUPS = 3
LOGGER_NAME = "steadyroute"


def configure(log_dir, to_stdout=False):
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers[:] = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if to_stdout:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        return logger
    os.makedirs(log_dir, exist_ok=True)
    main = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "router.log"), maxBytes=MAIN_LOG_BYTES,
        backupCount=MAIN_LOG_BACKUPS, encoding="utf-8")
    main.setFormatter(formatter)
    errors = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "router-error.log"), maxBytes=ERROR_LOG_BYTES,
        backupCount=ERROR_LOG_BACKUPS, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    errors.setFormatter(formatter)
    logger.addHandler(main)
    logger.addHandler(errors)
    install_exception_hooks(logger)
    return logger


def install_exception_hooks(logger):
    def main_hook(kind, value, tb):
        logger.error("unhandled exception", exc_info=(kind, value, tb))
    def thread_hook(args):
        logger.error("unhandled thread exception in %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    sys.excepthook = main_hook
    threading.excepthook = thread_hook
```

**修改 `weighted_router.py`**：
1. 常量 `LOG_DIR = os.environ.get("STEADYROUTE_LOG_DIR", "~/Library/Logs/Clash-Verge-Stability-Router")`。
2. `log(message)` 改为 `logging.getLogger("steadyroute").info(message)`；新增 `log_warning()`、`log_error()`。原来 `could not close connection`、`cycle failed` 改用 warning/error 级别。
3. `main()` 中：`--daemon` 时 `logging_setup.configure(LOG_DIR)`；否则 `configure(LOG_DIR, to_stdout=True)`（`--once`、`--status` 仍输出到终端）。

**修改 plist**（`deploy/macos/<LaunchAgent>.plist`）：`StandardOutPath` 改为 `.../bootstrap.log`，`StandardErrorPath` 改为 `.../bootstrap-error.log`。原因：launchd 持有的文件描述符不会跟随轮换，必须让 launchd 和应用写不同的文件。这两个文件只会记录日志系统初始化之前的崩溃，体积可以忽略。

**测试**（新建 `tests/test_logging_setup.py`）：
- `test_rotation_keeps_writing_current_path`：`MAIN_LOG_BYTES` 临时改成 2048，写入 10 KB 后 `router.log.1` 存在；再写一条唯一标记，标记出现在 `router.log` 中而不在 `router.log.1` 中。（不要比较 inode：文件系统可能复用被删除文件的 inode，比较结果不稳定。）
- `test_total_size_is_bounded`：写入 100 KB，目录总大小 ≤ 2048 × 6 + 余量。
- `test_warning_goes_to_error_log_only_once`
- `test_thread_exception_is_logged`：子线程抛异常，`router-error.log` 中有 traceback。
- `test_plist_does_not_point_launchd_at_rotating_files`：plist 中不包含 `router.log`。
- `tests/test_deploy.py` 里如果有断言 plist 内容，同步更新。

**提交**：`fix(logging): rotate application logs in-process`

### 任务 2.2 降低日志量

**规则**：
- 状态变化（`FAILOVER`、`OPTIMIZE`、隔离、休眠、本地断网、人工偏好、候选变化）**立即**记录；
- 每轮的 `probe:` 汇总：本轮有任何 `FAIL` 时立即记录，否则每 10 分钟最多 1 条；
- 每组的 `keep ...` 例行说明：内容与上一条相同则不写，只计数；内容变化或满 10 分钟时输出一条并附带"（此前重复 N 次）"。

**实现**：`logging_setup.py` 增加：

```python
class RateLimiter(object):
    """Suppress identical routine messages; emit on change or every `interval` seconds."""

    def __init__(self, interval=600, max_keys=64):
        self.interval = interval
        self.max_keys = max_keys
        self.state = {}

    def should_emit(self, key, message, now):
        entry = self.state.get(key)
        if entry is None or entry["message"] != message or now - entry["at"] >= self.interval:
            suppressed = 0 if entry is None else entry["suppressed"]
            if key not in self.state and len(self.state) >= self.max_keys:
                self.state.pop(next(iter(self.state)))
            self.state[key] = {"message": message, "at": now, "suppressed": 0}
            return True, suppressed
        entry["suppressed"] += 1
        return False, entry["suppressed"]
```

`weighted_router.py` 新增 `log_routine(key, message)`，用于 `evaluate_group()` 中所有 `keep ...`、`collecting weighted history`、`performance cooldown is active` 等例行说明；`probe:` 汇总用 key `"probe_summary"`，有 FAIL 时绕过限频直接 `log()`。

**时间比较注意**：分数、延迟每轮都在变，`keep %s; weighted score %.0f (best %.0f)` 这类消息每轮文本都不同，会导致限频失效。限频的比较内容要去掉数值：key 用 `(group_name, "keep_score")`，比较用的 message 只包含节点名和决策类型，完整文本（含数值）只在真正输出时写入。

**测试**：`test_rate_limiter_suppresses_identical`、`test_rate_limiter_emits_on_change`、`test_rate_limiter_emits_after_interval`、`test_rate_limiter_bounded_keys`、`test_probe_summary_with_failure_bypasses_limit`。

**验收**：模拟 100 个健康周期，`router.log` 新增行数 ≤ 5。

**提交**：`fix(logging): rate-limit routine probe and keep messages`

### 任务 2.3 客户端断开不写 traceback

**修改**：`weighted_router.py`

```python
EXPECTED_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class QuietHTTPServer(http.server.HTTPServer):
    def handle_error(self, request, client_address):
        kind = sys.exc_info()[0]
        if kind is not None and issubclass(kind, EXPECTED_DISCONNECTS):
            return
        logging.getLogger("steadyroute").exception("dashboard request failed")
```

`DashboardHandler` 中所有 `self.wfile.write(...)` 包在 `try/except EXPECTED_DISCONNECTS: return` 里。`start_dashboard()` 用 `QuietHTTPServer`。`scripts/benchmark-status.py` 使用 `router.http.server.HTTPServer`，保持不变即可。

**测试**：`test_broken_pipe_is_silent`（构造 `wfile.write` 抛 `BrokenPipeError` 的假 handler，错误日志为空）、`test_other_errors_are_logged`。

**提交**：`fix(dashboard): ignore expected client disconnects`

### 任务 2.4 当前内存与峰值内存

**新文件**：`src/steadyroute/runtime_metrics.py`（按 4.3 节接入）

```python
"""Process memory metrics without third-party packages."""

import ctypes
import ctypes.util
import resource
import sys

_TASK_VM_INFO = 22


class _TaskVMInfo(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("virtual_size", ctypes.c_uint64), ("region_count", ctypes.c_int32),
        ("page_size", ctypes.c_int32), ("resident_size", ctypes.c_uint64),
        ("resident_size_peak", ctypes.c_uint64), ("device", ctypes.c_uint64),
        ("device_peak", ctypes.c_uint64), ("internal", ctypes.c_uint64),
        ("internal_peak", ctypes.c_uint64), ("external", ctypes.c_uint64),
        ("external_peak", ctypes.c_uint64), ("reusable", ctypes.c_uint64),
        ("reusable_peak", ctypes.c_uint64), ("purgeable_volatile_pmap", ctypes.c_uint64),
        ("purgeable_volatile_resident", ctypes.c_uint64),
        ("purgeable_volatile_virtual", ctypes.c_uint64), ("compressed", ctypes.c_uint64),
        ("compressed_peak", ctypes.c_uint64), ("compressed_lifetime", ctypes.c_uint64),
        ("phys_footprint", ctypes.c_uint64),
    ]


def peak_rss_mb():
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return round(float(usage) / divisor, 1)


def current_footprint_mb():
    """Activity Monitor's 'Memory' value on macOS; None if unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        task = ctypes.c_uint.in_dll(libc, "mach_task_self_")
        info = _TaskVMInfo()
        wanted = ctypes.sizeof(info) // 4
        count = ctypes.c_uint(wanted)
        result = libc.task_info(task, _TASK_VM_INFO, ctypes.byref(info), ctypes.byref(count))
        if result != 0 or count.value < wanted:     # 内核未填到 phys_footprint
            return None
        return round(float(info.phys_footprint) / 1024.0 / 1024.0, 1)
    except Exception:
        return None
```

**修改 `weighted_router.py`**：
1. `memory_megabytes()` 改为调用 `runtime_metrics.peak_rss_mb()`（保持兼容）。
2. `run_cycle()` 每轮记录 `current_footprint_mb()`；每 10 分钟向 `state["memory_samples"]` 追加 `[int(now), 值]`，最多 144 个（24 小时）。
3. `build_status_snapshots()`：v1 和 legacy 的 `service` 增加 `memory_current_mb`、`memory_peak_mb`（`memory_mb` 保持为峰值，兼容旧客户端）；v1 增加 `memory_trend_mb_per_hour`：样本 ≥ 12 个时，用最早 6 个与最近 6 个的均值之差除以时间差（小时），否则为 `null`。
4. `dashboard.html`：顶部改为 `最近检测 ${age(a)} · 内存 ${cur} MB（峰值 ${peak} MB）`；"峰值内存"卡片改名为"内存占用"，大数字显示当前值，副标题显示"峰值 X MB · 当前进程生命周期"。当前值为 `null` 时显示"—"。

**测试**（`tests/test_runtime_metrics.py` 追加）：
- `test_footprint_returns_none_off_darwin`（mock `sys.platform = "linux"`）
- `test_memory_samples_bounded_to_144`
- `test_memory_trend_null_until_12_samples`
- `test_memory_trend_slope`：构造线性增长 0.5 MB/小时的样本，结果约为 0.5。
- CI 跑在 `macos-latest`，加一个 `@unittest.skipUnless(sys.platform == "darwin")` 的 `test_footprint_positive_on_darwin`。

**人工验收【你来执行】**：看板上的"当前"值与"活动监视器"中该 Python 进程的"内存"列相差不超过 1 MB；`footprint <PID>` 的 `phys_footprint` 与之一致。

**提交**：`feat(metrics): expose current footprint and peak RSS`

### 任务 2.5 阶段收尾

- CHANGELOG、`docs/OPERATIONS.md`（日志位置与上限、`bootstrap.log` 用途）、`docs/RISK_REGISTER.md`（R-04 改为"已控制"）、`.github/ISSUE_DRAFTS/003-bounded-logging.md` 勾选验收项。
- `scripts/status.sh` 的 Logs 部分增加总量统计：`du -ch "$LOG_DIR"/router*.log* | tail -1`。
- 版本 0.4.4，写 `docs/PERFORMANCE_0.4.4.md`。

**上线后验收【你来执行】**：
- 部署后，旧的 `router.log` 会被应用当作轮换起点继续使用；确认 `bootstrap.log` 基本为空；
- 运行 24 小时后，`router.log` 新增 < 200 KB；
- 一周后日志目录总量 ≤ 34 MiB，内存趋势 < 0.2 MB/小时。

**回滚**：完整包回滚。旧 plist 重新指向 `router.log`，不影响状态文件。

---

## 8. 阶段 3：选路策略 v2（P1）

**分支**：`feat/routing-policy-v2`

**阶段目标**：
- 当前节点偶发一次失败不会诱发性能切换（修复 F8）；
- 确认轮数基于新样本（修复 F9）；
- 门槛合理（修复 F10）；
- 旧故障按时间自然淡出（修复 F11）；
- 隔离可预期并有退避（修复 F12）；
- 线路明显劣化时 5 分钟内切走；
- 每组每 24 小时性能切换 ≤ 4 次（控制 R-07）。

### 任务 3.1 延迟样本窗口

**修改**：`update_node_stats()` 成功分支中追加：

```python
samples = list(node_state.get("latency_samples", []))
samples.append(int(delay))
node_state["latency_samples"] = samples[-SHORT_WINDOW_SIZE:]
```

`health_model.py` 追加：

```python
def latency_profile(node_state, min_samples=5):
    """Return {'p50', 'jitter', 'source'} or None. jitter = p90 - p50."""
    samples = [int(v) for v in node_state.get("latency_samples", []) if v is not None]
    if len(samples) >= min_samples:
        p50 = percentile(samples, 0.5)
        p90 = percentile(samples, 0.9)
        return {"p50": p50, "jitter": max(0.0, p90 - p50), "source": "window"}
    latency = node_state.get("latency_ewma")
    if latency is None:
        return None
    return {"p50": float(latency), "jitter": float(node_state.get("jitter_ewma", 0.0)), "source": "ewma"}
```

`latency_ewma`、`jitter_ewma` 继续计算，用于看板展示和样本不足时的回退。

**测试**：`test_latency_samples_bounded_to_short_window`、`test_latency_profile_window_vs_ewma_fallback`、`test_latency_profile_jitter_is_p90_minus_p50`。

**提交**：`feat(health): keep a bounded latency sample window`

### 任务 3.2 按时间衰减的长期可用率与延迟基线

`health_model.py` 追加：

```python
def _weight(age_hours, half_life_hours):
    return 0.5 ** (max(0.0, float(age_hours)) / float(half_life_hours))


def decayed_long_availability(buckets, now_hour, half_life_hours, fallback):
    num = den = 0.0
    for bucket in buckets:
        weight = _weight(now_hour - int(bucket.get("hour", now_hour)), half_life_hours)
        num += weight * int(bucket.get("success", 0))
        den += weight * int(bucket.get("total", 0))
    return num / den if den else float(fallback)


def decayed_baseline_latency(buckets, now_hour, half_life_hours):
    num = den = 0.0
    for bucket in buckets:
        successes = int(bucket.get("success", 0))
        if not successes:
            continue
        weight = _weight(now_hour - int(bucket.get("hour", now_hour)), half_life_hours)
        num += weight * int(bucket.get("latency_sum", 0))
        den += weight * successes
    return num / den if den else None
```

**修改 `weighted_router.py`**：
1. 新常量 `LONG_HALF_LIFE_HOURS = 6`。
2. `long_availability(node_state, now=None)` 改为调用 `decayed_long_availability(buckets, int(now // 3600), LONG_HALF_LIFE_HOURS, fallback=node_state.get("availability_ewma", 0.0))`，`now` 缺省用 `time.time()`。所有调用点传入可用的 `now`。
3. `eligible_for_optimization()` **删除** `availability_ewma >= MIN_AVAILABILITY` 这一条。保留：最近一次成功、样本 ≥ 10、连续成功 ≥ 3、短期窗口 ≥ 0.8、衰减长期可用率 ≥ 0.9、未隔离。
4. 看板"加权可用率"和 API 的 `availability_long` 自动变为衰减值（字段名不变，在 `docs/STATE_API.md` 说明语义变化）。

**测试**：
- `test_old_outage_fades`：20 小时前的桶 0/180（全部失败），最近 4 个小时的桶各 180/180 → 衰减可用率 ≈ 0.97，≥ 0.95（硬窗口算法为 0.80）。
- `test_recent_outage_dominates`：当前小时的桶 0/180，前 23 个小时的桶各 180/180 → 衰减可用率 ≈ 0.88，< 0.9（硬窗口算法为 0.96，会被误判为可用）。
- `test_baseline_latency_weighted_by_recency`
- `test_one_failure_no_longer_excludes_standby`：可用率 EWMA 0.8、其他条件满足 → 仍然 `eligible`。

**提交**：`feat(health): time-decayed long availability and latency baseline`

### 任务 3.3 新的分数与切换门槛

**常量调整**：

```python
MIN_ABSOLUTE_GAIN_MS = 100        # 原 180；改为与相对门槛取 max，不再取 AND
MIN_RELATIVE_GAIN = 0.30
AVAILABILITY_PENALTY_MS = 1000    # 原 4000；改为作用于衰减长期可用率
NOISE_MULTIPLIER = 2.0            # 改善量必须 ≥ 2 倍抖动
CANDIDATE_FRESH_SECONDS = 2 * PROBE_INTERVAL_SECONDS + CYCLE_BUDGET_SECONDS
MAX_PERFORMANCE_SWITCHES_PER_DAY = 4
```

**分数**（`update_node_stats()` 末尾替换原 `score` 计算）：

```python
profile = health_model.latency_profile(node_state)
if profile is None:
    node_state["score"] = 1000000.0
else:
    availability = long_availability(node_state, observed_at)
    node_state["score"] = (profile["p50"] + JITTER_WEIGHT * profile["jitter"]
                           + AVAILABILITY_PENALTY_MS * (1.0 - availability))
    node_state["latency_p50_ms"] = round(profile["p50"], 1)
    node_state["latency_jitter_ms"] = round(profile["jitter"], 1)
```

**门槛**（替换 `evaluate_group()` 1209–1218）：

```python
leader = min(eligible, key=lambda name: float(state["nodes"][name]["score"]))
cur, lead = state["nodes"][current], state["nodes"][leader]
current_score, leader_score = float(cur["score"]), float(lead["score"])
gain = current_score - leader_score
required = max(MIN_ABSOLUTE_GAIN_MS, MIN_RELATIVE_GAIN * current_score)
noise = NOISE_MULTIPLIER * max(float(cur.get("latency_jitter_ms", 0.0)),
                               float(lead.get("latency_jitter_ms", 0.0)))
fresh = now - int(lead.get("last_probe_at", 0)) <= CANDIDATE_FRESH_SECONDS
qualifies = leader != current and fresh and gain >= required and gain >= noise
```

**看板策略说明**（`dashboard.html` 的 `renderPolicy`）："最低绝对改善"和"最低相对改善"合并成一项：`max(${p.absolute_gain_ms} ms, ${Math.round(p.relative_gain*100)}%)`，说明文字为"改善门槛（取较大值）"。

**测试**（新建 `tests/test_routing_policy_v2.py`）：参数化验证下表。表中数值是分数（score），两个节点抖动都为 5 ms、可用率均为 1：

| 当前 | 候选 | 预期 |
|---|---|---|
| 57 | 40 | 不切（改善 17 < 100） |
| 120 | 60 | 不切（60 < 100） |
| 200 | 60 | 切（140 ≥ max(100, 60)） |
| 400 | 300 | 不切（100 < 120） |
| 400 | 250 | 切（150 ≥ 120） |

另外：
- `test_single_blip_does_not_trigger_switch`：当前 86 ms、候选 76 ms，两者先各积累 30 个成功样本；当前节点失败 1 次后连续成功 10 轮，冷却已结束，全程不切。（F8 的回归测试。旧代码在恢复后第 6 轮切换，已模拟验证。）
- `test_noise_rule_blocks_jittery_gain`：改善 150、抖动 90 → 不切。
- `test_stale_leader_is_not_confirmed`：候选 `last_probe_at` 在 60 秒前 → 不累计确认。
- 更新 `tests/test_weighted_router.py`、`tests/test_decision_event_flow.py` 的 `healthy()` 夹具，补上 `last_probe_at`（等于测试里 mock 的 `time.time()`）和 `latency_samples`，否则新鲜度规则会让原有测试失败。

**提交**：`feat(routing): robust cost and max(abs, rel) gain threshold`

### 任务 3.4 基于样本的确认、劣化快速通道和切换预算

1. **候选确认期每轮都测**：`choose_probe_targets()` 中，如果 `group_state["better_candidate"]` 存在，加入探测目标。
2. **确认必须有新样本**（替换 1229–1233）：
   ```python
   leader_sample_at = int(lead.get("last_probe_at", 0))
   if group_state.get("better_candidate") == leader:
       if leader_sample_at > int(group_state.get("better_sample_at", 0)):
           group_state["better_streak"] = int(group_state.get("better_streak", 0)) + 1
   else:
       group_state["better_candidate"] = leader
       group_state["better_streak"] = 1
   group_state["better_sample_at"] = leader_sample_at
   ```
3. **劣化快速通道**：
   ```python
   DEGRADED_RATIO = 2.0
   DEGRADED_MARGIN_MS = 100
   DEGRADED_SAMPLES = 3
   DEGRADED_COOLDOWN_SECONDS = 5 * 60
   DEGRADED_CONFIRMATIONS = 2
   ```
   `health_model.py` 追加：
   ```python
   def is_degraded(recent_samples, baseline, ratio, margin, needed):
       if baseline is None or len(recent_samples) < needed:
           return False
       threshold = max(ratio * baseline, baseline + margin)
       return all(float(value) >= threshold for value in recent_samples[-needed:])
   ```
   `evaluate_group()` 冷却判断之前计算：
   ```python
   baseline = health_model.decayed_baseline_latency(
       current_stats.get("long_buckets", []), int(now // 3600), LONG_HALF_LIFE_HOURS)
   degraded = health_model.is_degraded(
       current_stats.get("latency_samples", []), baseline,
       DEGRADED_RATIO, DEGRADED_MARGIN_MS, DEGRADED_SAMPLES)
   cooldown = DEGRADED_COOLDOWN_SECONDS if degraded else PERFORMANCE_COOLDOWN_SECONDS
   confirmations_required = DEGRADED_CONFIRMATIONS if degraded else PERFORMANCE_CONFIRMATIONS
   ```
   原来用 `PERFORMANCE_COOLDOWN_SECONDS`、`PERFORMANCE_CONFIRMATIONS` 的地方改用这两个局部变量。`group_state["latency_degraded"] = degraded`。
4. **切换预算**：性能回优执行前检查
   ```python
   recent = [t for t in group_state.get("performance_switch_times", []) if now - t < 86400]
   if len(recent) >= MAX_PERFORMANCE_SWITCHES_PER_DAY:
       log_routine((group_name, "budget"), "%s: keep %s; daily performance switch budget used" % (group_name, current))
       return
   ```
   故障切换不受预算限制，也不计入预算。
5. **状态投影**：`group_decision_facts()` 的 `confirmation_required` 改为本轮实际使用的轮数（需要把 `degraded` 存到 `group_state` 供其读取）；`current_degraded` 增加 `or group_state.get("latency_degraded")`，这样看板会显示已有的 `degraded`（"当前线路质量下降"）状态，不需要新增状态码。v1 group 的 `automation` 增加 `performance_switches_remaining_24h`。

**测试**（`tests/test_routing_policy_v2.py`）：
- `test_confirmation_needs_new_leader_sample`：同一 `last_probe_at` 连续评估 3 次，`better_streak` 保持 1。
- `test_better_candidate_probed_every_cycle`
- `test_degraded_fast_path`：基线 60 ms，当前最近 3 个样本都是 200 ms，候选 60 ms，上次切换在 6 分钟前 → 2 次有效确认后切换。
- `test_not_degraded_uses_normal_cooldown`：上次切换在 6 分钟前、未劣化 → 不切。
- `test_daily_budget_blocks_fifth_switch`
- `test_failover_ignores_budget`
- `test_degraded_state_surfaces_in_decision`：`decision.code == "degraded"`。

**提交**：`feat(routing): sample-based confirmation, degraded fast path, switch budget`

### 任务 3.5 隔离退避与半开放探测

**常量**：
```python
QUARANTINE_LADDER_SECONDS = (30 * 60, 60 * 60, 120 * 60)
QUARANTINE_RESET_SECONDS = 6 * 3600
```
`QUARANTINE_SECONDS` 保留为 `QUARANTINE_LADDER_SECONDS[0]`，供 API 兼容展示。

**重写 `record_health_window()` 中隔离部分**（849–857 行）：

```python
until = int(node_state.get("quarantine_until", 0))
in_quarantine = until and observed_at < until
half_open = until and observed_at >= until
if success:
    if half_open:
        node_state["quarantine_recovery_streak"] = int(node_state.get("quarantine_recovery_streak", 0)) + 1
    # 隔离期内的成功不计入恢复次数
else:
    node_state["quarantine_recovery_streak"] = 0
    if half_open:
        enter_quarantine(node_state, observed_at, escalate=True)   # 半开放期失败：立即升级隔离
        failures = []
    elif not in_quarantine:
        failures.append(int(observed_at))
        if len(failures) >= QUARANTINE_FAILURES:
            enter_quarantine(node_state, observed_at, escalate=False)
            failures = []
    # 隔离期内的失败不再延长隔离
```

```python
def enter_quarantine(node_state, now, escalate):
    released = int(node_state.get("quarantine_released_at", 0))
    level = int(node_state.get("quarantine_level", 0))
    max_level = len(QUARANTINE_LADDER_SECONDS) - 1
    if escalate or (released and now - released < QUARANTINE_RESET_SECONDS):
        level = min(level + 1, max_level)
    else:
        level = 0          # 首次隔离，或上次释放后已稳定 6 小时
    node_state["quarantine_level"] = level
    node_state["quarantine_until"] = int(now + QUARANTINE_LADDER_SECONDS[level])
    node_state["quarantine_recovery_streak"] = 0
    node_state["recent_failures"] = []
```

`is_quarantined()` 释放时写 `node_state["quarantine_released_at"] = int(now)`，其余不变（仍要求到期且半开放连续成功 3 次）。

**探测调度**：`choose_probe_targets()` 每组额外加入**一个**处于半开放期的节点（`quarantine_until <= now` 且恢复次数 < 3），这样半开放节点约 1 分钟就能完成恢复判断；隔离期内（未到期）的节点不探测。

**API**：节点增加 `quarantine_level`（0/1/2）和 `quarantine_seconds_current`。

**测试**：
- `test_first_quarantine_is_30_minutes`
- `test_requarantine_within_6h_escalates`：释放后 2 小时再次隔离 → 60 分钟；再次 → 120 分钟；再次 → 仍为 120 分钟。
- `test_level_resets_after_6h_healthy`
- `test_failures_during_quarantine_do_not_extend`
- `test_success_during_quarantine_not_counted_for_recovery`
- `test_half_open_failure_escalates_immediately`
- `test_half_open_node_probed_each_cycle`，`test_quarantined_node_not_probed`
- `state_contract.resolve_node_lifecycle()` 的 `half_open` 判断不变，跑一遍现有生命周期测试确认兼容。

**提交**：`feat(health): quarantine backoff with half-open probing`

### 任务 3.6 阶段收尾

- 看板策略说明区更新：隔离时长显示为"30 / 60 / 120 分钟（6 小时内重复故障逐级延长）"，新增"每日性能切换上限 4 次"和"劣化快速通道 5 分钟"。
- 更新说明文字（dashboard 第 36 行的 `<p>`）："先按短期故障、隔离状态和时间加权可用率筛选候选，再比较延迟中位数与抖动；质量分仅用于展示……"
- CHANGELOG（Changed 段写明门槛从"180 ms 且 30%"改为"max(100 ms, 30%)"）、`docs/ARCHITECTURE.md`（健康模型一节）、`docs/STATE_API.md`、`docs/RISK_REGISTER.md`（R-07 补充"每日切换上限"）。
- 新建 `docs/adr/0008-routing-policy-v2.md`：记录分数、门槛、衰减、预算的决策和取舍。
- 版本号与 `docs/PERFORMANCE_<版本>.md`。

**上线后观察【你来执行】**（至少 72 小时）：
- 每组性能切换次数 ≤ 4 次/天（看 `metrics.performance_switches_24h`）；
- 之前被 24 小时硬窗口压住的节点（如 65%～70% 的那几个），在无新故障的情况下半天内回到候选池；
- 没有出现"当前节点刚恢复就切走"的情况：`grep -B3 OPTIMIZE router.log` 查看切换前 3 行，当前节点不应有 FAIL。

**回滚**：完整包回滚。新增的状态字段（`latency_samples`、`quarantine_level` 等）旧版本会忽略；`quarantine_until` 语义兼容。

---

## 9. 阶段 4：被动检测（P2，先 shadow 后 active）

**分支**：`feat/passive-detection`

**思路**：Mihomo 在 warning 级别日志里记录连接失败，格式类似 `[TCP] dial <代理组名> (match <规则>) <来源> --> <目标:端口> error: <原因>`。日志只能定位到**代理组**，不能直接定位到节点，而我们知道每个组当前选的是哪个节点，所以可以换算过去。错误文本不稳定，所以被动信号**只触发即时复测**（任务 1.6 的 `confirm_current_failure`），最终结论仍由主动探测给出。

### 任务 4.0 【你来执行 + Codex】采集真实日志样本

1. Codex 编写只读脚本 `scripts/capture-mihomo-logs.py`：
   - 参数：`--socket`（必填，绝对路径）、`--seconds`（默认 600）、`--level`（默认 warning）、`--out`（输出 `.jsonl`）。
   - 通过 Unix socket 发送 `GET /logs?level=<level> HTTP/1.1`（不带 `Connection: close`），增量解析 chunked 响应，每行 JSON 写入文件。
   - **脱敏**：把 IPv4、IPv6 替换为 `<ip>`，来源端口替换为 `<port>`；目标域名保留（用于统计不同域名数）；不写入任何认证信息。
   - 单行超过 4096 字节丢弃；总行数上限 20000。
2. 【你来执行】在正常使用 AI 的时段运行 10 分钟；再临时断开一次当前台湾节点，运行 5 分钟。
3. 把输出放到 `tests/fixtures/mihomo_logs_normal.jsonl` 和 `tests/fixtures/mihomo_logs_node_down.jsonl`，**提交前人工检查**确认没有敏感信息（`check.sh` 的敏感信息扫描也会检查）。
4. 根据真实样本整理两张错误分类表，写入 `docs/adr/0009-passive-detection.md`：
   - 节点侧（计入）：例如超时、`deadline exceeded`、`no recent network activity`、握手或认证失败、连接代理服务器被拒绝；
   - 目标侧（不计入）：例如 DNS 解析失败、目标拒绝连接。
   以样本为准，不要凭记忆写正则。

**提交**：`chore(tools): capture sanitized mihomo log samples`

### 任务 4.1 日志流读取器

**新文件**：`src/steadyroute/log_watcher.py`（按 4.3 节接入）

职责与约束：
- 一个守护线程，通过 Unix socket 订阅 `/logs?level=warning`；
- 增量 chunked 解码，按行切分；单行上限 4096 字节，读缓冲上限 64 KB，超限丢弃并计数；
- 解析：只处理 `payload` 以 `[TCP] dial ` 或 `[UDP] dial ` 开头且包含 ` error: ` 的行。**代理组名可能含空格**（如"AI 台湾家宽线路"），所以不要按空格切分，而是用配置里的组名逐个匹配 `payload.startswith("[TCP] dial " + group_name + " ")`；
- 提取目标主机（`--> ` 与 ` error:` 之间，去掉端口）；按 4.0 的分类表判断是否为节点侧错误；
- 窗口：每组 10 个 1 秒桶，循环复用；每桶记录节点侧失败数和目标主机集合（上限 8 个）；时间用 `time.monotonic()`，不解析日志时间戳；
- 触发：10 秒内节点侧失败 ≥ 3 次且涉及 ≥ 2 个不同主机 → 标记该组 `suspect`，调用回调；同一组 15 秒内最多触发一次；
- 断线：标记 `status = "blind"`，按 1、2、4、8、16、30 秒退避重连；重连成功后清空窗口；
- 线程只写自己的数据（用 `threading.Lock` 保护），**绝不修改 `state`**。

对外接口：

```python
class LogWatcher(object):
    def __init__(self, socket_resolver, group_names, classify, on_suspect, clock=time.monotonic): ...
    def start(self): ...
    def stop(self): ...
    def snapshot(self): ...  # -> {"status": "streaming"|"blind"|"off", "dropped": int, "suspects": int}
    def reset(self): ...  # 休眠唤醒时调用
    def feed_line(self, raw_line): ...   # 供测试直接喂数据
```

**测试**（新建 `tests/test_log_watcher.py`，全部用 4.0 的 fixture 和 `feed_line`，不连 socket）：
- `test_group_names_with_spaces_are_matched`
- `test_target_side_errors_are_ignored`
- `test_suspect_requires_three_failures_two_hosts_in_10s`
- `test_single_host_retry_storm_is_not_suspect`
- `test_suspect_rate_limited_per_group`
- `test_window_slides_and_resets`
- `test_oversized_line_dropped_and_counted`
- `test_chunked_decoder_handles_split_chunks`：把一个 chunk 拆成 3 次 `recv` 喂入。
- `test_normal_fixture_produces_no_suspect`：正常时段样本不应触发。
- `test_node_down_fixture_produces_suspect`：断开节点样本应触发。

**提交**：`feat(passive): bounded mihomo log watcher`

### 任务 4.2 接入主循环（shadow 模式）

**常量**：`PASSIVE_DETECTION_MODE = "shadow"`（取值 `off` / `shadow` / `active`）。

1. `main()` 在 `--daemon` 且模式不为 `off` 时启动 `LogWatcher`；`on_suspect(group_name)` 回调只做两件事：把组名放进一个线程安全的集合 `PENDING_SUSPECTS`，然后 `WAKE_EVENT.set()`。
2. 主循环被唤醒时：
   - 如果是 **shadow**：不做额外探测，只记录事件 `PASSIVE_SUSPECT`（`reason_code` 为 `passive_dial_failures`），并在该组 `group_state["passive_pending"]` 记下时间。下一次正常周期如果确认故障，就把事件结果记为命中，否则记为误报。统计写入 `state["passive_stats"] = {"suspects": n, "hits": n, "misses": n}`，按 7 天滚动保留。
   - 如果是 **active**：对这些组立即执行一次**小周期**：`confirm_current_failure()` → 如果 `confirmed`，只对该组调用 `evaluate_group()`（热备已在任务 1.4 中保持新鲜）→ 保存状态、更新快照。同一组 15 秒内最多执行一次小周期。
   - 然后回到正常排期，**不改变**下一次正常周期的 `deadline`。
3. 休眠唤醒（任务 1.2 识别到 `slept`）时调用 `watcher.reset()`。
4. API：v1 `service` 增加 `passive_detection`：`{"mode", "status", "suspects_7d", "hits_7d", "precision_7d"}`（suspects 为 0 时 precision 为 `null`）。
5. `route-policies.json` 不改动；模式是代码常量，切换到 active 需要走发布流程。

**测试**：
- `test_shadow_mode_never_probes_or_switches_on_suspect`：唤醒后 `probe_url`、`select_node` 都不被调用。
- `test_active_mode_runs_mini_cycle_for_suspect_group_only`
- `test_mini_cycle_rate_limited`
- `test_regular_deadline_unchanged_by_wake`
- `test_passive_stats_hit_and_miss_accounting`
- `test_watcher_reset_on_resume`

**资源验收**：`benchmark-status.py` 加一项"启用 watcher 并喂入 10 万行"的 RSS 测量，增量 ≤ 1 MB。

**提交**：`feat(passive): wire log watcher in shadow mode`

### 任务 4.3 【你来执行】shadow 观察与启用

1. 发布 shadow 版本，正常使用至少 7 天，其间至少经历一次真实的节点故障。
2. 看 `/api/v1/status` 中的 `passive_detection.precision_7d`：
   - ≥ 0.8，且没有因被动信号导致的误切 → 进入第 3 步；
   - < 0.8 → 根据误报样本调整 4.0 的分类表和触发阈值，重新发布 shadow 版本继续观察。
3. 单独提交一个 PR，只把 `PASSIVE_DETECTION_MODE` 改为 `"active"`（提交信息：`feat(passive): enable active passive detection`），走正常发布流程。
4. 启用后验收：使用 AI 过程中断开当前节点，从断开到切换 ≤ 15 秒。

**回滚**：把常量改回 `"shadow"` 或 `"off"` 发一个 PATCH，或者直接回滚完整包。

---

## 10. 参数总表

| 参数 | 现值 | 新值 | 阶段 |
|---|---|---|---|
| 周期排期 | 耗时 + 20 秒 | 固定 20 秒 | 1 |
| `updated_at` 语义 | 周期开始 | 周期完成 | 1 |
| 数据过期阈值 | 60 秒 | 50 秒（2 × 20 + 10） | 1 |
| 基础探测超时 | 5000 ms | 3000 ms | 1 |
| 业务探测超时 | 8000 ms | 5000 ms | 1 |
| 探测线程数 | 6 | 10 | 1 |
| 故障确认 | 跨周期 2 次失败 | 同一周期内 3 次连续失败（首次 + 2 次复测，约 9 秒内） | 1 |
| 热备 | 无 | 每组 1 个，每轮探测 | 1 |
| 预检跳过 | 无 | 热备 120 秒内业务成功且本轮基础成功 | 1 |
| 站点故障判断 | 无 | 热备同 URL 也失败则判为站点故障，5 分钟 | 1 |
| 本地断网检测 | 无 | 失败时经 DIRECT 延迟接口探测 2 个国内可达 URL | 1 |
| 故障风暴 | 无 | 10 分钟 ≥ 3 次故障切换：强制完整预检 | 1 |
| 主日志 | 不轮换 | 5 MiB × 5 | 2 |
| 错误日志 | 不轮换 | 1 MiB × 3 | 2 |
| 例行日志 | 每轮 | 变化时或每 10 分钟 | 2 |
| 内存指标 | 峰值 RSS | 当前 footprint + 峰值 RSS + 趋势 | 2 |
| 分数 | EWMA 延迟 + 1.5 × EWMA 抖动 + 4000 × (1 − 按样本可用率) | p50 + 1.5 × (p90 − p50) + 1000 × (1 − 衰减可用率) | 3 |
| 切换门槛 | ≥ 180 ms **且** ≥ 30% | ≥ max(100 ms, 30%) 且 ≥ 2 倍抖动 | 3 |
| 候选新鲜度 | 无 | 最近样本 ≤ 50 秒 | 3 |
| 确认计数 | 按周期 | 按候选新样本 | 3 |
| 长期可用率 | 24 小时硬窗口 | 24 个小时桶，半衰期 6 小时 | 3 |
| 准入：样本 EWMA 可用率 ≥ 0.9 | 有 | 删除 | 3 |
| 劣化快速通道 | 无 | 最近 3 个样本 ≥ max(2 × 基线, 基线 + 100 ms)：确认 2 次、冷却 5 分钟 | 3 |
| 每日性能切换上限 | 无 | 每组 4 次（故障切换不计） | 3 |
| 隔离时长 | 固定 30 分钟，失败会延长 | 30 → 60 → 120 分钟，6 小时无故障重置；隔离期内失败不延长 | 3 |
| 半开放探测 | 随轮询 | 每轮 1 个 | 3 |
| 被动检测 | 无 | shadow → 精确率 ≥ 0.8 后 active | 4 |

保持不变：`PROBE_INTERVAL_SECONDS = 20`、`BUSINESS_PROBE_INTERVAL_SECONDS = 60`、`SHORT_WINDOW_SIZE = 20`、`SHORT_MIN_AVAILABILITY = 0.8`、`MIN_AVAILABILITY = 0.9`、`MIN_SAMPLES_FOR_OPTIMIZATION = 10`、`MIN_SUCCESS_STREAK = 3`、`PERFORMANCE_CONFIRMATIONS = 3`、`PERFORMANCE_COOLDOWN_SECONDS = 1800`、`MANUAL_HOLD_SECONDS = 3600`、`JITTER_WEIGHT = 1.5`、`QUARANTINE_FAILURES = 3`、`QUARANTINE_WINDOW_SECONDS = 600`、`QUARANTINE_RECOVERY_SUCCESSES = 3`。

---

## 11. 建议新建的 Issue

| 编号（建议） | 标题 | 对应任务 |
|---|---|---|
| #15 | 周期与切换指标 | 0.2 |
| #16 | 检测时效：固定速率调度与完成时间 | 1.1、1.2、1.8 |
| #17 | 故障快速切换：并行预检、热备、即时复测、断网与站点故障识别 | 1.3–1.7 |
| #3（已有草稿） | 有界日志与错误边界 | 2.1–2.3 |
| #18 | 当前内存与趋势 | 2.4 |
| #19 | 选路策略 v2 | 3.1–3.5 |
| #20 | 被动检测（shadow → active） | 4.0–4.3 |

每个 Issue 可以直接用 `.github/ISSUE_TEMPLATE/feature.yml` 或 `bug.yml`，正文粘贴对应任务的"目标、规则、测试、验收"部分。
