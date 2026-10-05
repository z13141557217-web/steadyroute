# 测试策略

## 一键检查

```bash
./scripts/check.sh
```

依次执行：`compileall` 编译 `src/`、`scripts/`、`sim/`、`tests/` 中的全部 Python 文件；
`unittest` 运行 `tests/` 下全部测试；`scripts/leak-scan.py` 扫描敏感信息。CI 在 macOS
（Python 3.9，与 `/usr/bin/python3` 一致）和 Ubuntu（Python 3.9、3.13）上运行同一脚本。

除真实内核测试外，所有测试只使用临时目录、假控制器和注入的钩子，不访问真实 Clash Verge、
`~/Library`、LaunchAgent 或网络。

## 真实内核测试（`tests/test_real_core.py`）

其余测试的对面是本项目自己写的控制器，只能证明代码符合我们对 Clash 的理解。这一组在一个真实的
Mihomo 内核上运行，回答只有内核自己能回答的问题：

- 内核肯打开哪些文件：运行配置不在内核 home 下时，按路径重载被拒绝（v0.5.1 在真实环境遇到的
  错误），以内容加载被接受。
- 配置检查（`-t`）的输出：原因在 `level=error` 行；缺某个数据库时点名的是哪条规则。
- 筛选：设置页用 Python `re` 预告的成员，与内核用自己的正则引擎选出的成员逐个地区比对；没有匹配
  时分组只有 `REJECT`。
- `/proxies` 与 `/rules` 的写法：内置出口（含 `PASS-RULE`）不被当作节点；规则类型与内容的格式。
- 完整流程：写入专线（规则排在最前、用户规则不动）、AI 分流体检、被旧配置覆盖后运行核对重新
  写入且保留选中的节点、关闭后内核与文件完全还原。
- 路由程序自己的 HTTP 客户端经 Unix socket 读写内核。

布局仿照 Clash Verge 的服务模式：内核 home 与运行配置分在两个目录，控制接口是 Unix socket。
不需要网络：geodata 是测试生成的极小 `GeoSite.dat`（`tests/geosite_fixture.py`），下载地址指向
本机关闭的端口。

内核由 `scripts/build-core.sh` 从固定的版本与提交构建（需要 git 和 Go），不进发布包：

```bash
./scripts/build-core.sh ~/.cache/steadyroute-core/mihomo
STEADYROUTE_MIHOMO=~/.cache/steadyroute-core/mihomo ./scripts/check.sh
```

没有设置 `STEADYROUTE_MIHOMO` 时这一组跳过。CI 在三种运行环境上都构建并运行，且设置
`STEADYROUTE_REQUIRE_CORE=1`：内核缺失或不可执行时测试失败，而不是跳过。升级所测的内核版本只需
修改 `build-core.sh` 里的版本与提交。

## 测试金字塔

### 单元测试

- 健康窗口与可用率、百分位、固定速率排期、休眠识别。
- 候选资格与分层决策；隔离、半开放和恢复。
- 故障切换与无损回优；手动选择保护、冷却和空候选。
- 国家与家宽识别（`regions.py`）；`auto_lock` 接管、锁定、改锁、切至家宽和排除分组。
- 状态 schema 与迁移；日志轮换和事件容量。
- AI 规则：net.coffee 规则优先且原样（含 NTP）、UDP 开启、内核版本检查、AI 专线只在故障时切换、升级只接管原有两条线、页面解析、同步安全检查、手动域名清洗。

### 集成测试

- 假 Unix Socket Mihomo 控制器：`/proxies`、`/connections`、delay、选择和关闭连接。
- 订阅新增、改名、消失和空集合；控制器断线、超时和格式错误。
- 候选集合两快照确认、单次 / 连续空组、组缺失、离线和畸形响应。
- 16、50、100 节点规模、事件 200 条上限。
- `tests/cycle_harness.py`：进程内可编排网络（节点断开、单次抖动、站点故障、本机断网），
  不使用 socket 与真实时间。

### API 与前端契约测试

- `/api/v1/status` schema；`/api/status` 与 `/api/v1/status` 来自同一缓存快照。
- 状态码与中文文案映射；`null`、过期状态和空列表。
- 看板不触发探测；浏览器中途断开不产生 traceback。
- 状态转换只生成一次结构化事件，重复轮询不生成事件。
- 回优链路覆盖确认 1/3、2/3、3/3、立即切换、有 / 无旧连接、观察结束、冷却和稳定。
- API、事件和订阅变化经过字段白名单，不泄露 URL、凭据、密码或服务器地址。
- Host 校验（421）、安全响应头；设置接口只接受本页发出的请求（见下文）。

## 安装器（`tests/test_installer.py`）

- 发布包与仓库目录结构一致；仓库顶层的 `install.command` / `uninstall.command` 就是包内文件；
  zip 保留可执行权限。
- 个人信息扫描：发布包干净，并能发现故意植入的本机路径、用户名、节点名、订阅链接和密钥。
- 全新安装、升级保留状态与设置、只保留 3 份备份、启动失败恢复上一版本、全新安装失败不留
  运行中的服务、端口占用、非 macOS 拒绝、缺少 Clash Verge 只提示。
- 旧版迁移：旧 LaunchAgent 停用、状态与日志带过来、固定设置转换为 `migration` 建议、迁移
  失败时旧服务重新启动、无关的 LaunchAgent 不受影响。
- 卸载：先停服务再撤销 Clash 改动；Clash 无法恢复时保留一切；安装目录中的模块能独立撤销。
- 从发布包安装并迁移；包内程序能加载默认设置。

## 设置与 AI 专线（`tests/test_settings.py`、`tests/test_ai_line.py`）

使用 `tests/fixtures/clash_verge/` 中脱敏的 `profiles.yaml`、扩展文件和运行配置，以及假内核
校验器和假控制器。

- 分组只含所选国家的家宽节点，`empty-fallback: REJECT`，永不回落 DIRECT；香港、澳门、俄罗斯、
  中国大陆不能作为 AI 专线。
- 按当前订阅解析 `option.groups` / `option.rules`；缩进变体；flow-style prepend 拒绝。
- 稳航区块总在用户自己的 prepend 规则之前；编辑与撤销不改动区块以外的任何内容。
- 迁移时旧分组被替换，关闭时原样放回；迁移同时写入两条线路和全部规则。
- 内核缺少数据库时去掉可选规则；重载失败时恢复全部文件；关闭专线恢复原状。
- 切换订阅被自愈检测到。
- 运行核对：专线在时不动 Clash；被旧配置覆盖后第二次核对时重新写入；只少一个分组也能发现；
  连不上内核不算缺失；写入被拒后按退避重试；写入无效时不再重复；一小时 6 次上限；设置页正在
  修改时让路；专线关闭后不核对。
- 预览不写任何文件；仅修改排除分组不触碰 Clash；非法输入不改变任何内容；未知设置项拒绝。
- 每周同步更新规则并重新写入；同步失败继续使用当前规则，6 小时后重试、连续 3 次失败后每天一次；立即同步；分组详情（锁定国家与状态）。
- 迁移建议一直显示到接受或忽略。
- AI 分流体检报告每个条目的命中规则、分组链路和出口。
- 只有同源、`Content-Type: application/json`、带 `X-SteadyRoute: 1` 的请求能修改设置；
  GET 接口正常返回。

## 故障快速切换与检测时效

- `tests/test_fast_failover.py`：同周期三次失败确认、瞬时失败不切换、复测不增加样本、本机
  断网冻结决策与恢复事件、每周期只做一次本机检测、永不选择 `DIRECT`、唤醒首轮只记成功、
  业务差分、预检并行与只重试失败 URL、热备选择与轮询、故障风暴。
- `tests/test_runtime_metrics.py`：周期耗时上限与分位数、24 小时切换计数、完成时间语义、
  50 秒过期阈值、固定速率主循环。
- 全量测试同时在 Python 3.9 与 3.13 下通过。

## 有界日志与内存

- `tests/test_logging_setup.py`：跨天轮换与压缩、单文件上限、超长单条截断、保留天数、总量
  上限（含压力写入）、压缩失败时继续写、四路分流、traceback 进错误日志、限频与去重。
- `tests/test_bounded_logging.py`：故障切换写入决策事件、状态事件只镜像一次、平稳周期限流、
  BrokenPipe 静默、内存采样间隔与上限、快照补丁不重建、探测线程池复用。

## 固定方案样本

退役的固定台湾 / 香港方案只保留为测试夹具 `tests/fixtures/route-policies.fixed.json`，用于
覆盖手写策略的校验、筛选矩阵（中文、繁体、emoji、地区与家宽词两种顺序；普通节点、其他
地区和信息节点排除）和迁移转换。`DIRECT`、`COMPATIBLE`、`REJECT`、`REJECT-DROP`、`PASS`、
`PASS-RULE` 在注册表二次防御中永远被拒绝。

## 模拟验收

[`sim/`](../sim/README.md) 在临时目录中并行运行两个版本，各连一个可编排的假 Mihomo 控制器，
用同样的剧本制造故障并对比切换时机、断开的连接和资源占用。它不进入发布包。

## 发布门槛

- `./scripts/check.sh` 全部通过。
- 发布包构建成功（个人信息扫描无命中）。
- 本机 `./install.command` 升级成功，看板报告新版本，至少完成一个检测周期。
- 涉及 Clash 写入的改动：在真实 Clash Verge 上预览、写入、体检、关闭各走一遍，确认关闭后
  配置与写入前一致。
- 性能或界面变化记录修改前后内存、空闲 CPU 和接口响应。

资源与接口对比的历史记录见 [docs/archive/performance/](archive/performance/)。
