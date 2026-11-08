# 节点表单行精简设计记录

用户已确认每节点单行，保留原有短柱条、状态胶囊、当前/热备标签。旧六列、多行辅助说明与删除柱条/胶囊的A/B设计已被否定。

本轮主表只回答节点身份、近期可靠性、最近检测、节点状态。长期数据和诊断信息下沉详情，恢复进度融入胶囊。完整名称单行占主要空间，窄窗口内部滚动；不改节点名，不在前端推导资格。

采用 Impeccable Operate/distill/craft-floor 文本参考与 Web Interface Guidelines 人工检查。未运行设计引擎、hooks、live或自动评分。最终布局规范见仓库根 DESIGN.md，验证见 NODE_TABLE_VALIDATION.md。
