# 架构说明

## 系统边界

```text
Clash Verge Rev / Mihomo
  ├─ 用户自己的订阅、分组与规则
  ├─ 每个订阅的扩展文件（option.groups / option.rules）
  ├─ Unix Socket 控制 API
  └─ 实际连接
           │
           ▼
SteadyRoute 后端（单进程，LaunchAgent com.steadyroute）
  ├─ auto_lock：接管分组、按国家锁定
  ├─ 探测调度、健康模型、决策状态机
  ├─ 原子状态持久化、有界事件 / 日志
  ├─ 只读缓存 API 与本地页面
  └─ 设置服务：AI 家宽专线、规则同步、AI 分流体检
           │
           ▼
本地看板与设置页（127.0.0.1:17654）
```

安装、升级和卸载由仓库侧的 `scripts/installer.py` 完成，不属于常驻进程，见
[DEPLOYMENT.md](DEPLOYMENT.md)。

## 模块

| 模块 | 责任 |
|---|---|
| `weighted_router.py` | 主循环、探测、选路、切换、HTTP 服务 |
| `auto_lock.py` | 每轮从控制器快照找出直接选中节点的 select 分组，按当前节点国家锁定，生成运行时策略 |
| `regions.py` | 从节点名识别国家、家宽和信息条目 |
| `route_policy.py` | 设置文件加载与校验、同国家家宽匹配 |
| `candidate_registry.py` | 候选发现、两快照确认和生命周期 |
| `health_model.py` | 短期 / 长期窗口、隔离与恢复、百分位、排期 |
| `state_contract.py` | 持久状态 schema、迁移、决策 / 生命周期文案和转换事件 |
| `node_catalog.py` | 只读节点目录（`/api/nodes`） |
| `logging_setup.py` | 有界日志和异常入口 |
| `runtime_metrics.py` | 内存与周期指标 |
| `settings_service.py` | 设置页后端：读取与修改设置、写入 / 撤销 Clash、运行核对、每周同步、每小时自愈、体检 |
| `diagnostics.py` | 诊断信息：把状态快照、设置与 `events.jsonl` 汇成一份文本报告，输出前遮盖路径与地址；只读 |
| `ai_line.py` | AI 专线与托管家宽线路：分组定义、写入计划、应用与撤销 |
| `ai_rules.py` | AI 规则：net.coffee 快照与同步安全检查、社区规则、进程规则、手动域名 |
| `ai_check.py` | AI 分流体检：只读遍历 Clash 当前规则并跟踪出口 |
| `clash_profile.py` | 定位当前订阅的扩展文件，编辑稳航管理区块，内核校验、备份、写入、重载、回滚 |

拆分原则：先补边界测试，再移动代码；不得为“文件更漂亮”制造运行风险。

## 自动锁定

稳航只有一种运行方式：`profile: auto_lock`。

```text
/proxies 快照
  → 找出直接选中节点的 select 分组（排除 exclude_groups 与 GLOBAL）
  → 按当前节点国家锁定；用户换到别的国家 → 改锁
  → 候选 = 该分组中同国家且名称标为家宽的节点
  → 生成与手写策略相同形状的运行时策略
  → 健康模型、故障切换、回优和三道同国家校验照常工作
```

- 设置文件 `config/route-policies.json` 不含手写策略；每轮从控制器快照重建。
- 没有家宽节点的国家只监控不切换；分组选的是另一个分组时暂停，锁定保留。
- 当前是同国家普通节点时，家宽成熟后无损换上（`adopt`）。
- 同国家铁律在加载策略、选路前和下发 `PUT` 前各校验一次；跨国家、机房和 `DIRECT` 一律拒绝。
- 每个分组可在 `auto_lock.group_business_urls` 指定自己的业务检测地址，否则用
  `auto_lock.business_test_urls`。

旧的固定台湾 / 香港方案（手写策略、静态节点名单、隐藏发现组）已退役，只作为测试夹具
`tests/fixtures/route-policies.fixed.json` 保留，见 [ADR-0008](adr/0008-unified-install-and-ai-line.md)。

## AI 家宽专线

可选功能。启用后，稳航在 Clash Verge 中建立一个只含所选国家家宽节点的分组，并把 AI 服务的
规则放在用户所有规则之前。安装本身从不修改 Clash；只有在设置页确认预览后才写入。

### 写入位置

Clash Verge Rev 为每个订阅保存两个扩展文件：`profiles.yaml` 中当前订阅的 `option.groups`
和 `option.rules` 各指向 `profiles/<uid>.yaml`，文件内是 `prepend` / `append` / `delete` 列表，
订阅更新时保留。稳航只在两个文件 `prepend` 列表顶部维护一个带标记的区块：

```text
# >>> SteadyRoute 稳航管理（请勿手动修改）
…
# <<< SteadyRoute 稳航管理
```

区块以外的内容从不改动。Clash Verge 只在重建配置时才合并扩展文件，因此同样的条目也写到
运行配置 `clash-verge.yaml` 顶部，再通过控制器重载；之后 Clash Verge 自己重建时，扩展文件
给出同样的结果。

### 写入事务

```text
计划（不写文件，供预览）
  → 用 Clash Verge 内核校验新运行配置（verge-mihomo -d <home> -f <staged> -t）
  → 核对运行配置文件里的分组与内核 /proxies 中的分组一致（不一致则不做任何改动）
  → 备份三个文件和 meta.json 到 clash-backups/（保留 10 份）
  → 原子写入扩展分组、扩展规则、运行配置
  → PUT /configs?force=true，配置内容放在请求体的 payload 里（不传文件路径）
  → 核对新分组已出现在 /proxies，恢复这些分组原来选中的节点
  → 内核拒绝新配置：内核仍在运行原配置，只恢复文件
  → 内核已加载后核对失败：恢复文件并把原配置重新交给内核
```

重载不传文件路径的原因：Mihomo 1.19 起内核只打开自身 home 目录（和 `SAFE_PATHS`）下的文件。
Clash Verge 以服务模式运行时，内核的 home 是
`/Library/Application Support/clash-verge-service/users/<uid>/runtime`，而 `clash-verge.yaml`
在用户目录的 `io.github.clash-verge-rev.clash-verge-rev` 下，按路径重载会被拒绝（HTTP 400，
`path is not subpath of home directory or SAFE_PATHS`）。以内容加载在两种模式下都可用。

内核缺少可选规则所需的数据库（`GEOSITE,category-ai-!cn`、`IP-ASN,399358`）时，去掉该规则
重新校验，并在设置页列出被丢弃的规则。写入结果记录在 `clash-applied.json`，包括被替换的
原有分组定义；关闭专线或卸载时据此原样恢复。

### 分组定义

```yaml
type: select
include-all-proxies: true
filter: 所选国家 AND 家宽（regexp2 先行断言）
exclude-filter: 到期、剩余流量等信息条目
exclude-type: direct
empty-fallback: REJECT
interrupt-exist-connections: false
```

`empty-fallback: REJECT` 保证没有可用节点时拒绝而不是直连（Mihomo 空组默认的 COMPATIBLE
等于直连）。按 net.coffee 开启 UDP 代理（不设 `disable-udp`），NTP 与 QUIC 同样从家宽出口。写入前检查内核版本 ≥ mihomo v1.19.27（`empty-fallback` 从该版本起生效）。AI 专线的策略带 `failover_only`：只在故障时切换，不做性能回优。香港、澳门、俄罗斯和中国大陆不在
ChatGPT / Claude 的服务地区内，不能作为 AI 专线国家。

### 规则优先级

全部指向专线分组，放在用户自己的规则之前：

1. ip.net.coffee 的 Claude 规则（22 条，含 `IP-CIDR,160.79.104.0/21`、
   `IP-CIDR6,2607:6bc0::/32`、`IP-ASN,399358`）和 ChatGPT / Codex 规则（`GEOSITE,openai`
   加 12 条）。程序附带 2026-09-30 的快照，每周同步；失败后 6 小时重试，连续失败 3 次后每天一次；设置页可立即同步（`POST /api/settings/sync`）。
2. `GEOSITE,category-ai-!cn`（社区汇总的其他 AI 服务）。
3. `PROCESS-NAME`：`Claude`、`Claude Helper`、`claude`、`ChatGPT`、`codex`。
4. 设置页手动添加的域名（`DOMAIN-SUFFIX`）。

net.coffee 的规则原样采用，包括最后的 `GEOSITE,category-ntp`（缺数据库时作为可选规则跳过并注明）。

每周同步的安全检查：每个来源 3–200 条规则、锚点域名必须存在、只允许安全的规则类型、单次
删除不超过一半。任一不满足时继续使用当前规则，并在设置页显示错误。

### 后台维护

一个独立线程，不占用探测线程：

- 每 20 秒（运行核对，`SettingsService.watch`）：读内核的 `/proxies` 与 `/rules`，写入过的分组必须
  都在，写入过的域名规则（DOMAIN / DOMAIN-SUFFIX / DOMAIN-KEYWORD）必须都在且指向专线分组。
  连续两次不符才重新写入（Clash Verge 可能正在重载）；写入失败按 1、2、4 … 30 分钟退避；
  写入成功但内核里仍然没有时停止自动写入；一小时内最多重新写入 6 次。状态经
  `GET /api/settings` 的 `line_status` 和 `GET /api/status` 各分组的 `line_state` 给出。
  依据是内核而不是文件：Clash Verge 用它内存里的旧配置重载时，扩展文件仍然是对的，内核却不再
  运行专线。
- 每小时：当前订阅变了或专线规则从运行配置文件中消失时，重新写入（自愈）。
- 每周：同步 net.coffee 规则，有变化时重新写入；通过 `POST /configs/geo` 更新 geodata。

所有对 Clash 的修改由同一把锁串行化。

### 旧版迁移

从固定方案升级时，安装器把旧设置转换为 `migration` 建议。用户在设置页确认后：“AI 台湾
家宽线路”成为 AI 专线（台湾），“香港家宽自动备援”成为托管家宽线路（香港），两者保留原名
并改为自动筛选；旧发现组被移除。确认前不写入。

### 专线的切换

专线是 `failover_only`：不做其他线路那种连续 3 轮确认的回优。两种情况会换节点，都只在同国家的
家宽节点之间：

- 节点故障：与其他线路相同（快速通道 5 秒探测、确认、关闭旧节点上的连接）。
- 节点持续变慢（`slow_exit`，v0.5.5，设置页可关）：每轮完整检测比较当前节点与评分最好的成熟
  备用节点，评分差达到回优门槛（180 ms 且 30%）的一轮记为“偏慢”。最近 45 轮（约 15 分钟）里
  有 30 轮偏慢、且当前这一轮也偏慢时，备用节点通过业务预检后切换，旧连接保留。手动选择保护期内
  与任何一次切换后的 30 分钟内记录清空；每条专线 24 小时内最多 3 次。门槛的取值见
  `docs/releases/v0.5.5.md` 所附数据：连续计数对抖动型慢节点反应太慢，窗口占比对偶发尖峰更稳。

### AI 分流体检

只读：取 Clash 当前规则（`/rules`），读取 Clash Verge 目录中的规则集文件以判断 `RULE-SET`，
为 34 条 net.coffee 条目逐条找出第一条命中的规则，沿分组链路找到实际出口节点和国家，标出
是否走了专线且国家正确。无法静态判断的规则（如其他 `GEOSITE`、`PROCESS-NAME`）列为不确定。

## 设置接口的安全边界

设置页是唯一能改变配置的入口。POST 只接受本机 Host、同源 `Origin`、
`Content-Type: application/json`、请求头 `X-SteadyRoute: 1`、请求体不超过 64 KB；可修改的
键只有 `exclude_groups`、`ai_line`、`manual`、`migration`。详见 [STATE_API.md](STATE_API.md)
与 [SECURITY.md](SECURITY.md)。

## 版本化状态投影

`state_contract.py` 负责持久状态 schema、确定性迁移、决策 / 生命周期文案和转换事件；
现有选路函数仍是执行行为的唯一入口。每轮探测和选路结束后，后端从同一份内存状态
投影一次缓存快照，并同时序列化 `/api/status` 兼容视图和 `/api/v1/status` v2 视图。
HTTP GET 只读取已编码字节，不连接 Mihomo、不扫描连接，也不触发切换。设置接口例外：
它们按请求读取控制器，但不参与选路。

持久状态和 API 独立版本化。v1 状态迁移前保存原子备份；损坏状态保存诊断副本后
以安全空状态重建；高于当前版本的状态立即拒绝，旧进程不能写回。详见
[状态契约](STATE_API.md)与 [ADR-0004](adr/0004-versioned-state-contract.md)。

## 探测调度

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

每 5 秒的快速通道只测当前节点；还没有成熟备用节点时顺带测最多 4 个未成熟候选。
探测共用一个常驻线程池（最多 10 线程）。两次完整周期之间，快速通道只把实时字段补丁到
最近一次完整快照上再编码，不重建快照。

## 日志

`logging_setup.py` 在进程拿到单实例锁之后安装处理器：`router.log`（INFO，突发限频）、
`router-error.log`（WARNING 以上，10 分钟去重）、`events.jsonl`（只收决策记录）和
`node-events.jsonl`（节点状态变化）。每个处理器按本地日期和单文件上限轮换、gzip 历史、
按天数与总量清理。`--once` 与交互运行输出到标准输出，不写文件。

## 数据所有权

- Git：源码、测试、默认设置、文档和发布元数据。
- 安装目录：程序副本、`state.json`、设置、`clash-applied.json`、`ai-rules.json`、
  `clash-backups/`，只在本机，不进入 Git。
- 日志：诊断信息，有界保留，不作为业务状态来源。
- 订阅与 Clash 配置：属于用户；稳航只改自己的标记区块，并能完整撤销。
- 发布包：由 Git 标签构建，可重建。

## 关键不变量

1. 看板刷新不能触发测速或路由切换。
2. 普通性能回优不得主动中断健康旧连接；真实故障才允许清理失效连接。
3. 只在锁定国家的家宽节点之间切换；没有安全候选时不得回落 DIRECT 或其他国家。
4. 状态、事件、趋势数据、日志和备份必须有容量上限。
5. 发现失败不得等价为空集合；只有确认的成功空快照可以移除全部候选。
6. 安装和升级不修改 Clash；对 Clash 的每次写入都先预览、经内核校验、可完整撤销。
7. AI 专线永不回落 DIRECT（内核版本不满足时拒绝写入）；UDP 与 NTP 与 TCP 同一出口。
