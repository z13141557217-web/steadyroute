# ADR-0001：仓库是唯一源码

状态：Accepted  
日期：2026-09-19

## 决策

使用 `/Users/nurture/Projects/steadyroute` 作为唯一开发源。`Library/Application Support` 中的文件仅由发布流程更新。

## 原因

直接编辑生产目录无法可靠审查差异、标记版本、回滚完整版本或区分源文件与运行状态。

## 后果

- 所有功能变更先进入仓库。
- 紧急生产修复必须反向同步。
- `state.json`、锁、日志和订阅不进入仓库。

