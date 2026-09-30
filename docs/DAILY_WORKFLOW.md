# 日常开发完整流程

## 一、记住这条主线

```text
Issue/需求
  → 功能分支
  → 编码与测试
  → Commit
  → Push
  → Pull Request
  → 自动检查
  → 合并 main
  → 更新版本与变更日志
  → 构建发布包
  → 本机 ./install.command 升级、观察
  → 发布 GitHub Release
```

`main` 是可发布版本；安装目录 `~/Library/Application Support/SteadyRoute` 不是开发目录。

## 二、开始一天的开发

```bash
cd ~/Projects/steadyroute
git status
git switch main
git pull --ff-only
./scripts/check.sh
```

预期：工作树干净、测试通过。如果 `git status` 显示陌生修改，先弄清来源，不要覆盖。

## 三、为一项工作建立分支

命名示例：

```text
feat/dynamic-candidates
fix/dashboard-broken-pipe
test/mihomo-controller
docs/release-runbook
refactor/state-store
```

创建：

```bash
git switch -c feat/dynamic-candidates
```

一个分支只解决一个主题。不要在同一分支同时做动态节点、视觉重做和日志重构。

## 四、让 Codex 开发

给 Codex 的任务必须包含：

- 仓库路径：`<仓库目录>`（例如 `~/Projects/steadyroute`）
- 当前分支和任务目标
- In Scope / Out of Scope
- 验收条件
- 必须运行的测试
- 不得直接修改安装目录
- 完成后不要自行运行 `./install.command`，除非本次明确授权

推荐提示：

```text
请在 ~/Projects/steadyroute 的当前功能分支开发。
不要修改安装目录、不要重启稳航、不要写入 Clash Verge 的配置。
先读取 README、docs/DEVELOPMENT.md、docs/ARCHITECTURE.md 和相关 ADR。
为本次问题建立回归测试，完成后运行 ./scripts/check.sh。
更新 CHANGELOG 和受影响文档，汇报修改文件、测试结果、风险和发布建议。
```

## 五、并行开发规则

不要让两个 Codex 任务同时修改同一个目录。

如果确实要并行：

1. 每项任务一个 Git 分支。
2. 每项任务一个独立 worktree。
3. 明确文件所有权。
4. 合并前逐个完成测试和审查。

例如：

```bash
git worktree add ../steadyroute-log-rotation -b feat/log-rotation main
git worktree add ../steadyroute-dashboard -b feat/dashboard-v2 main
```

禁止两个任务同时编辑 `weighted_router.py`。

## 六、开发过程中

随时查看：

```bash
git status
git diff
./scripts/check.sh
```

只暂存本次相关文件：

```bash
git add src/steadyroute/weighted_router.py tests/test_weighted_router.py CHANGELOG.md
git diff --cached
```

尽量避免不经检查直接 `git add .`。

## 七、提交

提交格式：

```text
<type>(<scope>): <简短说明>
```

类型：

- `feat`：新能力
- `fix`：修复
- `test`：测试
- `refactor`：不改变行为的结构调整
- `docs`：文档
- `chore`：工程维护

示例：

```bash
git commit -m 'feat(routing): discover residential candidates dynamically'
```

提交前 hook 会自动运行检查。失败时修复问题，不要用 `--no-verify` 绕过。

## 八、推送与 Pull Request

```bash
git push -u origin feat/dynamic-candidates
gh pr create --fill
```

Pull Request 需要说明：

- 为什么改
- 改了什么
- 没改什么
- 测试证据
- 对已安装服务和用户 Clash 配置的风险
- 发布和回滚方法

即使只有一个人，也建议使用 PR；它提供清晰的审查页面和 CI 记录。

## 九、审查与合并

检查：

- CI 是否通过
- 是否存在敏感信息
- 是否误改无关规则
- 是否补测试
- 状态 schema 是否兼容
- 是否增加资源占用
- 是否能回滚
- 是否可能改动用户的 Clash 配置；若会，是否有校验、备份和完整撤销

合并后：

```bash
git switch main
git pull --ff-only
git branch -d feat/dynamic-candidates
```

远程分支可在 PR 合并时自动删除。

## 十、版本与发布

不是每个 commit 都发布。准备发布时：

1. 决定 PATCH、MINOR 或 MAJOR。
2. 更新 `VERSION`。
3. 把 `Unreleased` 内容整理到新版本下，写好 `docs/releases/v<版本>.md`，并在
   `src/steadyroute/changelog.html` 增加该版本。
4. 运行完整检查。
5. 提交版本变更，合并到 `main`。
6. 创建并推送 tag。
7. 构建发布包，本机升级，发布 Release。

示例：

```bash
git tag -a v0.5.1 -m 'SteadyRoute v0.5.1'
git push origin main
git push origin v0.5.1
python3 scripts/build_package.py
```

完整步骤见 [RELEASE.md](RELEASE.md)。

## 十一、本机升级

```bash
./install.command
```

安装器会停止旧服务、备份当前版本、替换程序文件、保留设置和状态、启动新版本并确认看板
报告的版本；失败时自动恢复上一版本。随后在看板观察至少一个检测周期。不要把零散文件
手工复制到安装目录。详见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 十二、紧急修复

```bash
git switch main
git pull --ff-only
git switch -c fix/short-description
```

先恢复服务，再在同一工作周期补：

- 回归测试
- 根因记录
- CHANGELOG
- PATCH 版本
- 回滚验证

## 十三、每周管理

每周更新：

- `docs/PROJECT_STATUS.md`
- `docs/BACKLOG.md`
- `docs/RISK_REGISTER.md`
- 周报模板

复盘：

- 本周是否直接改过安装目录
- 是否有未提交变更
- 是否有失败测试被绕过
- 日志和备份是否超限
- 是否需要发布或升级演练
- 风险是否升级

## 十四、常用命令速查

查看工作树状态、修改和最近的提交历史：

```bash
git status
git diff
git log --oneline --decorate --graph -20
```

完整检查（编译、全部测试、敏感信息扫描）：

```bash
./scripts/check.sh
```

查看已安装服务的状态：

```bash
python3 scripts/installer.py status
```

构建发布包 `dist/SteadyRoute-v<版本>.zip`：

```bash
python3 scripts/build_package.py
```

安装或升级本机：

```bash
./install.command
```

同步远程主干、推送当前分支：

```bash
git switch main && git pull --ff-only
git push -u origin HEAD
```

## 十五、绝对不要做

- 不要把订阅 URL、令牌、密码、`state.json`、日志、真实节点名或本机路径提交到 Git。
- 不要在 `main` 上进行大规模试验。
- 不要让多个任务同时编辑同一文件。
- 不要使用 `--no-verify` 绕过检查。
- 不要手工改安装目录或 Clash Verge 扩展文件中稳航管理的区块。
- 不要把私有仓库改为公开，除非完成完整安全审计。
- 不要把 GitHub 当作运行状态和订阅凭据的备份。

