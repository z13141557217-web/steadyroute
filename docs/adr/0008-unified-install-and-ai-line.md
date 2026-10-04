# ADR-0008：统一安装路径与可选 AI 家宽专线

状态：Accepted  
日期：2026-09-30（v0.5.1）

## 决策

稳航只保留一个产品、一条安装路径：

- 选路只有 `auto_lock`：接管 Clash 中直接选中节点的 select 分组，按当前节点国家锁定，只在
  该国家的家宽节点之间切换。固定台湾 / 香港方案（手写策略、静态节点名单、隐藏发现组、
  group enhancement 部署工具）退役，只作为测试夹具保留。
- `./install.command`（`scripts/installer.py install`）负责安装、升级、换电脑和从任何旧版
  迁移；从 git clone 和从发布包运行完全相同。取代 ADR-0003 的原子部署工具链和 ADR-0006 的
  group enhancement 事务。
- 安装从不修改 Clash。AI 家宽专线是设置页中的可选功能：写入 Clash Verge 当前订阅扩展文件中
  带标记的稳航区块和运行配置，每次写入都经内核校验、备份、重载核对，失败全部回滚；关闭或
  卸载时完整恢复原状，包括迁移时替换的旧分组。
- AI 规则以 ip.net.coffee 为第一优先级，随程序附带快照并每周带安全检查同步。

## 原因

“作者自用的固定方案”和“给他人安装的自动锁定方案”并存，意味着两套配置、两套部署、两套
文档，而真实差异只是“是否要一条专供 AI 的家宽线路”。把这一差异变成一个可选、可撤销的设置，
就能让每台 Mac 走同一条经过测试的路径。

发现组必须写进用户订阅才能工作，这让“部署稳航”与“修改 Clash”纠缠在一起，需要人工重载和
额外验证。自动锁定直接读取用户已有分组，安装因此可以完全不碰 Clash；需要改 Clash 的只剩
专线，而专线的每次改动都能先预览、再校验、再撤销。

## 后果

- 删除旧的部署、回滚、打包、状态查看脚本，发现组的生成、管理、验证与 Mihomo 配置校验脚本，
  Clash 分组部署模块，LaunchAgent 模板，以及 `config/clash-verge/groups.yaml` 和
  `config/route-policies.json`。仓库只保留 `config/route-policies.default.json`。
- 安装目录统一为 `~/Library/Application Support/SteadyRoute`，LaunchAgent 为
  `com.steadyroute`；旧 LaunchAgent 在新服务健康后移到 `SteadyRoute-backups/legacy/`，旧程序
  目录保留。
- 旧版的台湾 / 香港线路在用户确认后分别成为 AI 专线和托管家宽线路，名称不变、改为自动筛选。
- 发布包为 `dist/SteadyRoute-v<版本>.zip`，与仓库目录结构相同，打包前扫描个人信息。
- 设置页引入第一个写接口，需要同源、JSON、自定义请求头等额外防护（见 SECURITY.md）。
