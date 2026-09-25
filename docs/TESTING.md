# 测试策略

## 测试金字塔

### 单元测试

- 健康窗口与可用率。
- 候选资格与分层决策。
- 隔离、半开放和恢复。
- 故障切换与无损回优。
- 手动保持、冷却和空候选。
- 状态 schema 与迁移。
- 日志轮换和事件容量。

### 集成测试

- 使用假的 Unix Socket Mihomo 控制器。
- `/proxies`、`/connections`、delay、选择和关闭连接。
- 订阅新增、改名、消失和空集合。
- 控制器断线、超时和格式错误。
- 状态原子保存和损坏恢复。
- discovery group `all` 的两快照确认、单次/连续空组、组缺失、离线和畸形响应。
- 动态增加、批量增加、改名、删除非当前/当前、全部删除与重启恢复。
- 16、50、100 节点规模、事件 200 条上限和 shadow 零 PUT。

### API/前端契约测试

- `/api/v1/status` schema。
- 状态码与中文文案映射。
- `null`、过期状态和空列表。
- 浏览器中途断开不产生 traceback。
- 看板不触发探测。
- `/api/status` 与 `/api/v1/status` 来自同一缓存快照。
- 固定 fixture 覆盖全部代理组状态、全部节点生命周期、`null`/`0` 和过期状态。
- 状态转换只生成一次结构化事件，重复轮询不生成事件。
- 真实回优链路覆盖确认 1/3、2/3、3/3、立即切换、有/无旧连接、观察结束、冷却和稳定；
  同周期事件必须有序补齐中间语义，非法组/节点转换必须失败。
- v1→v2、损坏恢复、迁移失败与未来版本拒绝写回。
- API、事件和订阅变化经过字段白名单，不泄露 URL、凭据、密码或服务器地址。

### 生产冒烟测试

- 服务运行。
- 本地端口响应。
- Mihomo 控制器可达。
- 两个代理组存在且当前节点有效。
- 最近周期未过期。
- CPU、RSS 和接口耗时在基线范围内。

## 发布门槛

- 所有自动测试通过。
- Mihomo 配置校验通过。
- 无敏感信息扫描命中。
- dry-run 至少完成一个周期。
- 有版本化备份和回滚命令。
- 性能或界面变化记录修改前后 RSS、空闲 CPU、单次 API 和 100 次刷新结果。
- `generate-groups.py --check` 必须证明增强配置与单一 JSON 源无漂移。
- 安装了当前 Mihomo 核心时必须对临时独立配置执行 `-t -f` staged 校验。
- v0.4.0 发布包中的策略模式必须是 `shadow`。
- discovery group 必须生成 `hidden: true`；当前 Mihomo staged 校验通过，API 仍返回
  `hidden/all`。Clash Verge Rev 前端源码会过滤 `group.hidden`，桌面验收不得出现重复发现组。
- 人工偏好测试必须同时证明：健康节点不因普通性能差异提前切换，真实故障绕过偏好并
  清除计时，旧六小时状态封顶为一小时，故障决策优先于 `manual_hold` 文案。
- group enhancement 控制面必须在临时 `profiles.yaml`/`profiles/` 上验证默认 dry-run、
  current `option.groups` 解析、越界/宽泛/符号链接拒绝、单一来源与 staged 校验、备份 SHA、
  原子 apply、故障自动恢复、rollback 和 `/proxies` discovery 完整性。

## 故障快速切换与检测时效（v0.4.3）

- `tests/cycle_harness.py` 提供进程内可编排网络（节点断开、单次抖动、站点故障、本机断网），
  不使用 socket 与真实时间；`tests/test_fast_failover.py` 覆盖同周期三次失败确认、瞬时失败
  不切换、复测不增加样本、本机断网冻结决策与恢复事件、每周期只做一次本机检测、永不选择
  `DIRECT`、唤醒首轮只记成功、业务节点侧/站点侧差分、预检并行与只重试失败 URL、新鲜跳过、
  热备选择与轮询、故障风暴。
- `tests/test_runtime_metrics.py` 覆盖周期耗时上限与分位数、24 小时切换计数、完成时间语义、
  50 秒过期阈值、固定速率主循环（周期耗时不累加到间隔）和看板时效源码约束。
- `tests/test_health_model.py` 覆盖纯函数：百分位、固定速率排期、休眠识别。
- 全量测试同时在 Python 3.9（生产 `/usr/bin/python3`）与 3.11 下通过。

## 有界日志与内存（v0.4.4）

- `tests/test_logging_setup.py`：跨天轮换与压缩、跨天重启、单文件上限、超长单条截断、
  保留天数、总量上限（含 3000 条压力写入）、压缩失败时继续写、三路分流、traceback 进
  错误日志、旧日志一次性迁移与 90 天清理、限频/去重/例行限流及其内存上限、plist 不再指向
  应用轮换文件。
- `tests/test_bounded_logging.py`：故障切换写入决策事件（含同地区目标与快速通道标记）、
  状态事件只镜像一次且重启不重写、平稳周期 keep 行限流、失败汇总每次都记、BrokenPipe
  静默与真实 socket 请求、内存采样间隔/上限/随进程重置、斜率、历史轮数一次性合并、
  快照补丁不重建且不改状态、探测线程池复用、看板文案约束。
- 加速压力模拟（进程内真实 `run_cycle` + 真实文件日志 + 假时钟）：一周正常运行与两天
  8% 随机丢包风暴，记录各文件大小。
- `sim/run_scenarios.py --soak`：两个版本并行 10 分钟平稳运行，记录 RSS 与 CPU 时间。

## 独立验收看板

候选进程只读提供 `/acceptance`，数据来自随包固定的
`fixtures/status_contract_v2.json`。页面展示全部样例和声明的转换路径，不读取真实
订阅、不调用控制器、不修改生产看板。它用于候选验收，不是第二个常驻进程。

本轮资源与接口对比见 [v0.3.0 性能记录](PERFORMANCE_0.3.0.md)。
动态候选影子对比见 [v0.4.0 性能记录](PERFORMANCE_0.4.0.md)。
人工偏好与隐藏发现组对比见 [v0.4.2 性能记录](PERFORMANCE_0.4.2.md)。

## v0.4.0 筛选固定样本

- 当前 11 条台湾、5 条香港家宽静态样本全部纳入。
- 普通台湾/香港、其他地区和信息节点排除。
- 中文、繁体、emoji、地区/住宅词两种顺序覆盖。
- `3x`、`10x`、CF、HY2、VLESS 不作为排除条件。
- `DIRECT`、`COMPATIBLE`、`REJECT`、`REJECT-DROP`、`PASS`、`PASS-RULE`
  在注册表二次防御中永远被拒绝。

## 部署集成测试

`tests/test_deploy.py` 在临时目录构造发布包、目标目录、备份目录、LaunchAgent plist、
假的 `launchctl` 和文件型状态响应。它不连接 Mihomo、不绑定生产端口、不修改真实配置。
测试验证默认 dry-run 零写入、备份元数据与 SHA-256、原子切换、`bootout` 失败、
旧服务或监听端口未退出、复制旧状态未产生新周期、危险与重叠路径、激活后故障注入
自动恢复、独立回滚和脏工作树拒绝。

`tests/test_clash_group_deploy.py` 只使用 `TemporaryDirectory` 和假 Mihomo core，不读取或
写入当前机器的 Clash Verge profiles。真实重载属于管理窗口人工步骤，测试不伪造
“已加载”结果。

`tests/fixtures/profiles_nested_selected.yaml` 保留真实 profiles 的脱敏结构：`items:` 使用
顶层 sequence，remote item 的 `selected` 与其他嵌套列表位于 `option` 之前。回归测试
证明只有固定 item 缩进的 `- uid:` 会开启新 profile，并拒绝重复 current、重复 uid、
重复/歧义 `option.groups`、奇数缩进、flow-style items 和复杂 option。
