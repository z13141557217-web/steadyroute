# 开发流程

## 分支模型

采用轻量主干开发：

- `main` 始终表示可发布状态。
- 每项工作使用 `feat/...`、`fix/...`、`refactor/...` 或 `docs/...` 分支。
- 一项功能一个分支、一个明确验收目标。
- 合并前必须通过 `./scripts/check.sh`。

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
- [ ] 无订阅地址、密码、令牌或生产状态进入 Git
- [ ] 更新变更日志
- [ ] 更新相关运维/架构文档
- [ ] 描述发布风险与回滚方法
- [ ] 生产验证指标明确

## 紧急修复

允许在生产故障时先恢复服务，但必须在同一工作周期完成：

1. 保存故障证据。
2. 最小修复。
3. 回归验证。
4. 把生产修复同步回仓库。
5. 补测试和变更日志。
6. 发布 PATCH 版本。

