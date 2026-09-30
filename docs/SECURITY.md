# 安全与隐私

## 数据分类

| 数据 | Git | 日志 | 看板 |
|---|---|---|---|
| 源码与模板 | 允许 | 不需要 | 不显示 |
| 节点显示名称 | 仅限虚构样例 | 可记录 | 可显示 |
| 订阅 URL、令牌、密码 | 禁止 | 禁止 | 禁止 |
| 节点服务器地址 | 默认禁止 | 默认禁止 | 禁止 |
| 连接目标域名 | 最小化 | 仅诊断摘要 | 默认不显示 |
| `state.json`、`clash-applied.json`、`ai-rules.json` | 禁止 | 不复制 | 经脱敏后展示 |
| 本机路径、用户名 | 禁止（文档用通用路径） | 允许（本机） | 不显示 |

## 控制面

- 看板只绑定 `127.0.0.1`，且只服务 Host 为 `127.0.0.1:<端口>` 或 `localhost:<端口>`
  的请求，其他（包括缺少 Host）返回 `421`，防 DNS rebinding（v0.4.6）。
- 只读页面和接口只接受 GET。唯一的写入口是设置接口 `POST /api/settings/preview` 与
  `POST /api/settings/apply`（v0.5.1），只在同时满足以下条件时接受：本机 Host、同源
  `Origin`、`Content-Type: application/json`、请求头 `X-SteadyRoute: 1`、请求体不超过 64 KB。
  自定义请求头和 JSON 类型会触发 CORS 预检，服务从不应答预检，因此其他网页无法发起修改。
- 设置接口只能修改 `exclude_groups`、`ai_line`、`manual`、`migration`；手动域名逐条校验。
- 不提供网页端节点切换、服务停止或状态清空。
- 不加载外部脚本、字体或 CDN。
- 所有响应经同一个出口：页面 CSP 为 `default-src 'self'` 加 `base-uri`、`form-action`、
  `frame-ancestors` 均为 `'none'`；接口 CSP 为 `default-src 'none'`；统一 `no-store`、
  `nosniff`、`no-referrer`，不发送任何 CORS 放行头（v0.4.6）。
- `/api/status`、`/api/v1/status`、固定 fixture 和结构化事件只允许字段白名单；禁止
  订阅 URL、令牌、凭据、密码和服务器地址，即使这些字段意外进入持久状态也不得投影。
- 节点 UI ID 只使用代理组名与显示名称的 SHA-256 稳定摘要；改名生成新 ID，不继承旧历史。
- 动态候选事件只发布稳定 ID、状态、reason 和时间；策略正则和节点显示名仅在专用缓存
  视图出现，不发布订阅 URL、服务器地址或完整代理对象。
- 分组 `all` 只读取名称数组；SteadyRoute 不保存 `/proxies` 中的服务器或认证字段。

## 账号与出口安全

- AI 服务登录、聊天、上传和长连接必须进入同一国家的出口；启用 AI 家宽专线时由专线规则保证。
- 普通回优使用会话粘滞，避免频繁出口跳变。
- 地区识别异常、共享滥用严重或属性不明节点应降级或隔离。
- 任何规则变化都要验证没有 DIRECT 泄漏和错误地区出口。
- 选路只在锁定国家的家宽节点之间进行；内置 DIRECT/COMPATIBLE/PASS/REJECT 名称在注册表
  再次过滤，不能成为节点身份。
- AI 家宽专线使用 `empty-fallback: REJECT`（永不直连）、`exclude-type: direct` 和
  UDP 代理开启（按 net.coffee，NTP、QUIC、WebRTC 与 TCP 同一出口）；内核低于 mihomo v1.19.27 时拒绝写入；香港、澳门、俄罗斯、中国大陆不能作为 AI 专线国家。
- AI 规则同步只接受安全的规则类型：DOMAIN 系列、IP-CIDR / IP-CIDR6（IPv4 前缀不短于 /12，
  IPv6 不短于 /24）、IP-ASN 和 GEOSITE（`cn`、`private`、`geolocation-!cn`
  等宽泛分类一律拒绝）。每个来源规则数 3–200、锚点域名必须存在、单次删除不超过一半；
  不满足时保留当前规则。

## Git 与发布包安全

- 提交前检查敏感 URL、令牌和密钥：`scripts/leak-scan.py` 经 git 列出文件，只用标准库，
  无法运行时退出码为 2（不会静默通过），报告中不打印匹配到的值。
- `.gitignore` 含 `*secret*`、`*credentials*` 等模式；测试会检查源码目录下没有被忽略的
  非缓存文件，避免新文件因命名被悄悄漏提交。
- 文档和代码使用通用路径（如 `~/Projects/steadyroute`、`<仓库目录>`），不写本机用户名、
  真实节点名或分组名。
- `scripts/build_package.py` 打包前扫描本机用户路径、用户名、个人节点或分组名、订阅链接、
  密钥和邮箱，并拒绝 `state.json`、`route-policies.json`、`clash-applied.json` 等本机数据
  文件；任一命中都不生成 zip。
- 添加或更改远程仓库前必须确认其可见性和历史中无敏感信息。

## 安装与 Clash 写入安全

- 安装器只写当前用户的 `~/Library` 下的固定位置，LaunchAgent 以当前用户运行，不需要管理员
  权限。
- 升级前备份当前版本；新版本 40 秒内未报告正确版本即恢复上一版本或重新启动旧服务。旧版
  LaunchAgent 只在新服务健康后才停用，旧程序目录保留。
- 安装和升级从不修改 Clash。写入 Clash 只在设置页预览并确认后发生，之后的自愈和每周同步
  只重新应用已确认的设置。
- 只编辑 Clash Verge 当前订阅扩展文件中带标记的稳航区块和运行配置顶部的对应条目；拒绝符号
  链接、缺失的扩展文件和无法安全解析的 YAML 写法。
- 每次写入：内核校验 → 备份 → 原子写入 → 重载 → 核对 → 失败全部恢复。备份保存在
  `clash-backups/`，只在本机，沿用原文件权限。
- 卸载先停止服务，再撤销 Clash 中的全部稳航条目并放回被替换的原有分组；无法撤销时不删除
  任何文件。
- 版本备份和 Clash 备份可能包含运行状态和订阅配置，必须保留在用户本机，不得提交到 Git。
