# ADR-0006：当前订阅的 group enhancement 独立原子部署

状态：Accepted  
日期：2026-09-20

## 决策

稳航应用目录部署与 Clash Verge 当前订阅的 group enhancement 分为两个事务。仓库侧工具
从显式注入的 `profiles.yaml` 读取 current remote subscription 和 `option.groups`，只允许
目标位于同级非符号链接 `profiles/` 目录。命令默认 dry-run；apply 前验证 JSON 单一来源、
shadow 模式和 Mihomo staged 配置，随后保存带 SHA/元数据的原件并原子替换。失败自动恢复，
rollback 同样默认 dry-run。

工具不自动触发 Clash Verge 重载。管理窗口人工重载后，必须从 Unix socket `/proxies`
只读确认所有 discovery groups 存在，才能开始影子观察。

## 原因

把生成文件只复制到稳航应用目录不会改变当前订阅的 `option.groups` 绑定；后端会持续看到
`GROUP_MISSING`，因此无法完成真实影子验收。自动猜测 UID、直接覆盖 profiles 或假设文件
替换已经被 Mihomo 加载都会扩大生产风险。

## 后果

- group enhancement 有独立备份、回滚和审计元数据。
- 发布步骤多一个明确人工重载点，但不会暗中影响真实流量。
- 未通过 `/proxies` 验证时不得声称 discovery 生效或开始 12–24 小时观察。
