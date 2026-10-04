# 项目状态

更新时间：2026-10-04

## 当前阶段

`v0.5.1`（“大一统”）：稳航合并为一个产品、一条安装路径。

- 选路只保留 `auto_lock`：接管 Clash 中直接选中节点的分组，按国家锁定，只在该国家家宽
  之间切换。固定台湾 / 香港方案退役，只作为测试夹具保留。
- `./install.command` 统一负责安装、升级、换电脑和从旧版迁移，失败自动回滚；
  `./uninstall.command` 完整撤销。
- 新增设置页 `/settings`：接管范围、可选的 AI 家宽专线（写入 Clash Verge，内核校验、
  失败回滚、可完整撤销）、手动 AI 域名、旧版迁移确认、AI 分流体检。
- 发布包改为 `dist/SteadyRoute-v<版本>.zip`，与仓库目录结构一致，打包前扫描个人信息。

## 已完成的里程碑

| 版本 | 内容 |
|---|---|
| 0.1.0–0.2.0 | 独立仓库、工程治理、原子部署与回滚 |
| 0.3.0 | 状态 schema v2、决策状态机、`/api/v1/status` |
| 0.4.0–0.4.2 | 候选自动发现（影子模式）、手动选择保护 |
| 0.4.3–0.4.6 | 5 秒快速通道、有界日志、看板重做、安全加固 |
| 0.5.0 | 按国家自动锁定、首个安装包 |
| 0.5.1 | 统一安装与迁移、设置页、AI 家宽专线 |
| 0.5.2 | 修复 Clash Verge 服务模式下专线无法写入 |
| 0.5.3 | 运行核对：对照 Clash 正在运行的内容，专线被覆盖后自动重新写入 |

## 待办与风险

- v0.5.2 已在真实 Clash Verge（服务模式）上通过：安装与迁移、专线写入、34 / 34 体检、出口 IP、
  DNS、WebRTC。关闭专线与卸载的撤销尚未在真实环境做过。
- 测试对面仍是自己写的模拟 Clash；在 CI 里运行真实 mihomo 内核尚未做（见 #6）。
- Clash Verge 何时用内存里的配置覆盖内核、覆盖时是否同时改写 `clash-verge.yaml`，没有在真实环境
  观察过；v0.5.3 的运行核对按“内核为准”处理，文件与内核不一致时会显示原因而不是强行写入。
- `scripts/publish-release.sh` 改为调用 `scripts/build_package.py`。
- 后续工作见 [ROADMAP.md](ROADMAP.md) 与 [RISK_REGISTER.md](RISK_REGISTER.md)。

## 决策记录

- 仓库作为唯一源码，安装目录只由安装器更新（[ADR-0001](adr/0001-source-of-truth.md)）。
- 运行时坚持 Python 标准库、单进程、本机看板（[ADR-0002](adr/0002-runtime-dependencies.md)）。
- 持久状态 schema 与 API schema 独立版本化，新旧 API 共享同一缓存快照
  （[ADR-0004](adr/0004-versioned-state-contract.md)）。
- 统一安装路径与可选 AI 专线（[ADR-0008](adr/0008-unified-install-and-ai-line.md)）。
