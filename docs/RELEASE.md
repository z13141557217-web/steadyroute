# 发布与版本管理

## 环境

| 环境 | 说明 |
|---|---|
| 开发 | Git 工作目录，允许测试和修改 |
| 发布包 | 从已打标签的 commit 构建的 `dist/SteadyRoute-v<版本>.zip` |
| 已安装 | `~/Library/Application Support/SteadyRoute`，只由 `./install.command` 更新 |

## 版本来源

一个版本由以下内容共同确定，必须一致：

- `VERSION`
- Git tag，例如 `v0.5.1`
- `CHANGELOG.md` 中的 `## [<版本>]` 小节
- `docs/releases/v<版本>.md`：GitHub Release 说明，面向使用者
- `src/steadyroute/changelog.html`：看板“更新日志”页

运行中的服务在 `/api/status` 的 `service.version` 报告版本；安装器以它判断新版本是否启动成功。

## 发布流程

1. 功能分支合并到 `main`，工作树干净。
2. 更新 `VERSION`、`CHANGELOG.md`、`docs/releases/v<版本>.md` 和 `changelog.html`。
3. 运行完整检查：

   ```bash
   ./scripts/check.sh
   ```

4. 提交版本变更，创建并推送标签：

   ```bash
   git tag -a v0.5.1 -m 'SteadyRoute v0.5.1'
   git push origin main
   git push origin v0.5.1
   ```

5. 构建发布包：

   ```bash
   python3 scripts/build_package.py
   ```

6. 在本机升级并观察至少一个检测周期：

   ```bash
   ./install.command
   ```

7. 发布 GitHub Release：

   ```bash
   ./scripts/publish-release.sh
   ```

## 发布包

`scripts/build_package.py` 生成 `dist/SteadyRoute-v<版本>.zip`，目录结构与仓库相同，只包含
安装器需要的内容：`VERSION`、`src/steadyroute/` 中的程序文件、
`config/route-policies.default.json`、`scripts/installer.py`、`install.command`、
`uninstall.command` 和生成的 `使用说明.txt`。

打包前的门禁：

- 默认设置必须是 `auto_lock` 且没有手写策略。
- 不得包含 `state.json`、`route-policies.json`、`clash-applied.json`、`ai-rules.json`、
  `router.lock` 等本机数据文件。
- 扫描个人信息：本机用户路径、用户名、个人节点或分组名、订阅链接、密钥、邮箱。任一命中
  都拒绝打包并列出位置。
- `.command` 文件在 zip 中保留可执行权限。

## GitHub Release

`scripts/publish-release.sh [版本]` 从标签在临时 worktree 中重新构建发布包，再用 `gh` 创建
或更新 Release，说明取自 `docs/releases/v<版本>.md`，附件为该版本的 zip。只有最新的标签
标为 Latest。补发旧版本时传入版本号：

```bash
./scripts/publish-release.sh 0.5.1
```

需要先安装并登录 GitHub CLI：

```bash
brew install gh
gh auth login
```

## 回滚触发条件

- 服务无法持续运行，或安装器健康检查失败。
- 代理组丢失、回落 DIRECT，或出现其他国家出口。
- 状态文件无法读取、迁移失败或未来 schema 被拒绝。
- AI 专线写入后 Clash 无法加载配置，或 AI 分流体检显示流量未走专线。
- 选路行为与发布前测试不一致。
- 内存、CPU 或接口延迟明显回退。

## 回滚原则

- 安装器在启动失败时自动恢复上一版本，不需要人工介入。
- 主动退回时，用目标版本（v0.5.1 及以后）的发布包或标签重新运行 `./install.command`。
- 不在安装目录拼接多个版本的文件；未知未来 schema 必须保留原文件，并安装能识别它的版本。
- Clash 侧的改动在设置页关闭专线即完整撤销；安装和升级本身从不修改 Clash。
- 回滚后在看板确认服务正常，并保留失败版本的日志用于复盘。

详见 [DEPLOYMENT.md](DEPLOYMENT.md)。
