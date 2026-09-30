# ADR-0001：仓库是唯一源码

状态：Accepted  
日期：2026-09-19（2026-09-30 随 v0.5.1 更新路径）

## 决策

Git 仓库（本机克隆位置不限，例如 `~/Projects/steadyroute`）是唯一开发源。安装目录
`~/Library/Application Support/SteadyRoute` 中的文件只由安装器（`./install.command`）更新。

## 原因

直接编辑生产目录无法可靠审查差异、标记版本、回滚完整版本或区分源文件与运行状态。

## 后果

- 所有功能变更先进入仓库。
- 紧急修复也在仓库中完成，再经安装器安装到本机。
- `state.json`、设置、锁、日志、订阅和 Clash 备份不进入仓库。

