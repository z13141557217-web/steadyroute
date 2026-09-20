# ADR-0005：配置驱动的动态候选先以影子模式发布

状态：Accepted  
日期：2026-09-20

## 决策

以标准库可读的 `config/route-policies.json` 作为候选策略唯一来源。它生成 Mihomo active
与 discovery 组，并驱动 SteadyRoute 的组、地区、业务探测、预热和退役参数。Mihomo
以 `include-all-proxies`、`filter`、`exclude-filter`、`exclude-type` 和
`empty-fallback: REJECT` 筛选；SteadyRoute 仅在连续两次成功读取 discovery group
`all` 后改变正式动态集合。

v0.4.0 固定 `mode=shadow`：动态集合只发现、对账、预热、展示和记录，执行器继续使用
静态候选。`mode=active` 需要独立审批、RSS 增量不超过 1 MB，并由后续版本发布。

## 原因

订阅名称会新增、改名和删除，静态名单会漏收或保留失效节点；但控制器重载的瞬时空组
又不能被当作全部删除。Mihomo 原生筛选减少重复规则，稳定确认和影子阶段隔离错误匹配
对真实选路的影响。

## 后果

- 新地区只增加策略数据，不增加地区业务分支。
- 控制器离线、组缺失和畸形响应保留最后有效集合。
- warming 不参与回优；空集合进入 `no_candidate` 并由 REJECT fail-closed。
- 节点改名创建新身份，旧身份退役 24 小时后清理。
- 生产接管、跨地区降级和策略后台不属于 v0.4.0。
