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
  → 备份、部署、冒烟、观察
  → 必要时回滚
```

`main` 是可发布版本；生产目录不是开发目录。

## 二、开始一天的开发

```bash
cd /Users/nurture/Projects/steadyroute
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

- 仓库路径：`/Users/nurture/Projects/steadyroute`
- 当前分支和任务目标
- In Scope / Out of Scope
- 验收条件
- 必须运行的测试
- 不得直接修改生产目录
- 完成后不要自行部署，除非本次明确授权

推荐提示：

```text
请在 /Users/nurture/Projects/steadyroute 的当前功能分支开发。
不要修改生产目录或重启稳航。
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

禁止两个任务同时编辑生产 `weighted_router.py`。

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
- 对生产的风险
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

合并后：

```bash
git switch main
git pull --ff-only
git branch -d feat/dynamic-candidates
```

远程分支可在 PR 合并时自动删除。

## 十、版本与发布

不是每个 commit 都部署。准备发布时：

1. 决定 PATCH、MINOR 或 MAJOR。
2. 更新 `VERSION`。
3. 把 `Unreleased` 内容整理到新版本下。
4. 运行完整检查。
5. 提交版本变更。
6. 创建 tag。
7. 构建发布包。

示例：

```bash
git tag -a v0.2.0 -m 'SteadyRoute v0.2.0'
git push origin main
git push origin v0.2.0
./scripts/build-release.sh
```

在自动部署脚本完成前，不要手工把零散文件复制到生产；应按照 `docs/RELEASE.md` 先备份、校验并准备完整回滚。

## 十一、生产发布

标准顺序：

```text
检查工作树与 tag
  → 构建发布包与 SHA-256
  → 生产备份
  → 暂存目录语法检查
  → 原子替换
  → 重启 LaunchAgent
  → 冒烟测试
  → 观察一个检测周期
  → 记录发布结果
```

任何关键检查失败，立即恢复上一个完整版本，不在生产目录临时拼修。

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

- 本周是否直接改过生产
- 是否有未提交变更
- 是否有失败测试被绕过
- 日志和备份是否超限
- 是否需要发布或回滚演练
- 风险是否升级

## 十四、常用命令速查

```bash
# 当前状态
git status

# 查看修改
git diff

# 查看提交历史
git log --oneline --decorate --graph -20

# 完整检查
./scripts/check.sh

# 查看生产状态
./scripts/status.sh

# 构建发布包
./scripts/build-release.sh

# 同步远程主干
git switch main && git pull --ff-only

# 推送当前分支
git push -u origin HEAD
```

## 十五、绝对不要做

- 不要把订阅 URL、令牌、密码、`state.json` 或日志提交到 Git。
- 不要在 `main` 上进行大规模试验。
- 不要让多个任务同时编辑同一生产文件。
- 不要使用 `--no-verify` 绕过检查。
- 不要在没备份、没回滚方案时部署。
- 不要把私有仓库改为公开，除非完成完整安全审计。
- 不要把 GitHub 当作生产状态和订阅凭据的备份。

