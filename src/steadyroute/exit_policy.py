"""AI exit authorization and live evidence. No controller writes or inferred user intent."""

MODES = ("locked", "failover_only", "adaptive")
LABELS = {"locked": "固定节点", "failover_only": "仅故障备援", "adaptive": "自动择优"}


def mode(line):
    # Old false still permits failover. Old true/absent retains the old slow-exit behavior.
    return line.get("exit_policy") or ("adaptive" if line.get("slow_exit", True) else "failover_only")


def validate(line):
    if "exit_policy" in line and line["exit_policy"] not in MODES:
        raise ValueError("ai_line.exit_policy must be locked, failover_only or adaptive")
    if "exit_policy" in line and "slow_exit" in line:
        raise ValueError("exit_policy replaces slow_exit; do not save both")
    if mode(line) == "locked":
        node = line.get("locked_node")
        if not isinstance(node, str) or not node.strip() or "```" in node or "\n" in node:
            raise ValueError("固定节点需要明确的 locked_node（不支持换行或三个反引号）")


def authorizes(line, reason):
    policy = mode(line)
    if policy == "locked":
        return False  # even restoration is an explicit settings operation, never a router switch
    return policy == "adaptive" or reason in ("confirmed_failure", "removed")


def status(line, proxies=None, rules_ok=False, probe_ok=None, connections=()):
    configured = mode(line)
    result = {"configured_mode": configured, "effective_mode": None,
              "locked_node": line.get("locked_node"), "policy_status": "disabled",
              "reason": "AI 专线未启用", "legacy": "exit_policy" not in line,
              "label": LABELS[configured]}
    if not line.get("enabled"):
        return result
    if proxies is None:
        result.update(policy_status="controller_offline", reason="控制器离线，无法验证实际约束")
        return result
    if configured != "locked":
        result.update(effective_mode=configured if rules_ok else None,
                      policy_status="active" if rules_ok else "unverified",
                      reason="可能改变出口IP，存在账号风控风险")
        return result
    group = proxies.get(line.get("group_name")) or {}
    target = line.get("locked_node")
    leaf = proxies.get(target) or {}
    present = bool(leaf.get("type")) and "all" not in leaf
    expected = [target] if present else ["REJECT"]
    if not rules_ok or group.get("type") != "Selector" or group.get("all") != expected:
        result.update(policy_status="boundary_unverified", reason="固定边界未核实；请检查配置与 AI 分流规则")
    elif group.get("now") != expected[0]:
        result.update(policy_status="selection_drift", reason="外部选择漂移；未更新锁定目标")
    else:
        result.update(effective_mode="locked", policy_status=("locked" if present else "missing_reject"),
                      reason="仅允许指定节点；不可连接时拒绝" if present else "锁定节点消失；受管 AI 连接拒绝")
        if present and probe_ok is False:
            result.update(policy_status="probe_abnormal", reason="锁定节点探测异常；保持目标，不清理健康会话")
        inherited = sum(1 for connection in (connections or ())
                        if line.get("group_name") in (connection.get("chains") or [])
                        and target not in (connection.get("chains") or []))
        result["inherited_connections"] = inherited
        if inherited:
            result.update(effective_mode=None, policy_status="inherited_sessions",
                          reason="新连接已约束；%d 条历史连接仍在其他出口，等待自然结束" % inherited)
    return result
