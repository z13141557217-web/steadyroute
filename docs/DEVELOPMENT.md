# 开发流程

## 对话分工

需求、验收和发布管理与具体开发分开；先按仓库根 `AGENTS.md` 的交接规则确认授权与生产边界，再实施和测试。

## 分支模型

### 分支与工作树命名约定

用户确认后，后续所有新建分支与工作树统一遵循以下约定。

- 分支使用 `<type>/<short-description>`，工作树目录使用 `steadyroute-<type>-<short-description>`。
- type 使用 `feat`、`fix`、`refactor`、`docs`、`test` 或 `chore`。
- 描述使用简洁的小写英文，以连字符分词，表达变更目的。
- 有明确 Issue 时可使用 `<type>/<issue-number>-<short-description>`，目录同步加入编号。
- 版本号只用于专门的发布分支，例如 `release/0.7.0`，目录为 `steadyroute-release-0.7.0`。
- 禁止使用中文、空格、模糊的临时名称和 `new`、`final`、`test1` 等后缀。
- 已有历史分支保留原名，当前工作经核对后可改名，生产安装目录保持固定路径。

当前 P0 故障计时工作的分支为 `feat/failover-observability`，工作树目录为
`steadyroute-feat-failover-observability`。命名更改应同步检查预览服务、自动任务和文档中的路径引用。

采用轻量主干开发：

- `main` 始终表示可发布状态。
- 每项工作使用 `feat/...`、`fix/...`、`refactor/...` 或 `docs/...` 分支。
- 一项功能一个分支、一个明确验收目标。
- 合并前必须通过 `./scripts/check.sh`。
- 开发只在仓库中进行；安装目录 `~/Library/Application Support/SteadyRoute` 只由
  `./install.command` 更新。

当前 GitHub 套餐不会对私有个人仓库强制执行 Ruleset。仓库因此使用本机 `pre-push` 钩子阻止直接推送 `main`，并以功能分支、Pull Request 和 GitHub Actions 作为实际门禁。升级到支持私有仓库规则强制执行的套餐后，再增加远端 Ruleset。

紧急情况下只有在完成风险说明后，才允许一次性覆盖：

```bash
STEADYROUTE_ALLOW_MAIN_PUSH=1 git push origin main
```

覆盖变量只对单条命令有效，不得写入 shell 配置。

## 提交流程

1. 明确需求、非目标和验收条件。
2. 先增加或更新测试。
3. 实现最小变更。
4. 运行自动检查。
5. 更新文档和 `CHANGELOG.md`。
6. 判断 SemVer 影响。
7. 使用 Conventional Commits 提交。

示例：

```text
feat(routing): discover residential candidates dynamically
fix(dashboard): ignore expected client disconnects
test(state): cover empty-candidate fail-closed behavior
docs(runbook): add local rollback procedure
```

## 完成定义

- [ ] 需求和验收条件明确
- [ ] 有自动测试或说明无法自动化的原因
- [ ] 所有检查通过
- [ ] 无订阅地址、密码、令牌、运行状态、真实节点名或本机路径进入 Git
- [ ] 若改动会写入用户的 Clash 配置：有预览、内核校验、备份、失败回滚和完整撤销，并有测试
- [ ] 同步看板使用说明和相关 README/使用文档
- [ ] 同步仓库 CHANGELOG、看板更新日志和对应版本说明
- [ ] 核对 VERSION、提交、远端分支与 PR 描述一致；同一未发布候选内不无故升版
- [ ] 交付逐项报告上述同步结果，缺项不得报告完整交付
- [ ] 获授权发布时再核对 Tag、GitHub Release、安装包及校验文件，推送不等于授权合并或安装
- [ ] 更新相关运维/架构文档
- [ ] 描述发布风险与回滚方法
- [ ] 本机 `./install.command` 升级后的验证指标明确

## 紧急修复

允许在生产故障时先恢复服务，但必须在同一工作周期完成：

1. 保存故障证据。
2. 最小修复。
3. 回归验证。
4. 修复只在仓库中进行，经 `./install.command` 安装到本机。
5. 补测试和变更日志。
6. 发布 PATCH 版本。
