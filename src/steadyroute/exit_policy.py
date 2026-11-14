"""AI exit authorization and live evidence. No controller writes or inferred user intent."""

import ipaddress
import os
import time
import datetime

try:
    import ai_check
except ModuleNotFoundError:  # pragma: no cover
    from . import ai_check

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


def managed_connection(connection, group, rules):
    """Recognize existing AI sessions even when their chain predates the managed group.

    Only use the applied AI rules and observable metadata; a provider/group name containing
    'AI' is not membership evidence. GEOSITE/ASN need an exact recorded rule hit when no
    directly testable domain, address or process is available. Never query or change routing.
    """
    if group in (connection.get("chains") or []):
        return True
    metadata = connection.get("metadata") or {}
    host = str(metadata.get("host") or "").lower().rstrip(".")
    process = metadata.get("process") or os.path.basename(metadata.get("processPath") or "")
    recorded = (ai_check.norm(connection.get("rule")), str(connection.get("rulePayload") or "").lower())
    for rule in rules or ():
        parts = str(rule).split(",")
        if len(parts) < 3 or parts[2] != group:
            continue
        kind, value = ai_check.norm(parts[0]), parts[1]
        if recorded == (kind, value.lower()):
            return True
        if host and ai_check.domain_hit(kind, value, host):
            return True
        if kind == "processname" and process and process == value:
            return True
        if kind in ("ipcidr", "ipcidr6"):
            try:
                if ipaddress.ip_address(metadata.get("destinationIP")) in ipaddress.ip_network(value, strict=False):
                    return True
            except ValueError:
                pass
    return False


EVIDENCE_MAX_AGE = 50
COVERAGE_NOTE = "仅覆盖按受管组、已应用规则及可用元数据识别的 AI 连接；未识别连接不判违规，节点链不证明公网 IP 地区。"
STALE_SUMMARY = "核验证据已过期；规则与连接路径需重新核对"
UNAVAILABLE_SUMMARY = "核验读取失败；规则与连接路径未核实"


def connection_snapshot(response):
    """Mihomo explicitly emits connections:null for a successfully observed empty table."""
    if not isinstance(response, dict) or "connections" not in response:
        return None
    value = response["connections"]
    return [] if value is None else value if isinstance(value, list) else None


def path_kind(connection, target, proxies):
    """Mihomo chains are terminal first. A group-only/absent chain proves no exit."""
    chains = connection.get("chains")
    if not isinstance(chains, list) or not chains or not all(isinstance(v, str) and v for v in chains):
        return "unknown"
    leaf = chains[0]
    if leaf == "DIRECT":
        return "foreign"
    proxy = proxies.get(leaf) or {}
    if not proxy.get("type") or "all" in proxy or leaf in ("REJECT", "COMPATIBLE", "PASS"):
        return "unknown"
    return "target" if leaf == target else "foreign"


def age_status(evidence, now=None):
    """Downgrade cached conclusions without querying a controller or renewing their age."""
    now = time.time() if now is None else now
    if (evidence.get("configured_mode") != "locked" or evidence.get("expires_at") is None
            or evidence.get("policy_status") in ("disabled", "controller_offline", "evidence_stale")):
        return evidence
    timestamps = [evidence.get("checked_at"), (evidence.get("connections") or {}).get("checked_at")]
    if now <= evidence["expires_at"] and all(at is None or at <= now + 1 for at in timestamps):
        return evidence
    result = dict(evidence, effective_mode=None, boundary_verified=False, rules_status="stale",
                  policy_status="evidence_stale", severity="warning", reason=STALE_SUMMARY, summary=STALE_SUMMARY,
                  headline="核验证据已过期")
    result["connections"] = dict(evidence["connections"], status="stale")
    result["evidence_detail"] = "证据已过期。" + evidence.get("evidence_detail", COVERAGE_NOTE)
    return result


def status(line, proxies=None, rules_ok=False, probe_ok=None, connections=None, managed_rules=(),
           now=None, checked_at=None, connections_checked_at=None):
    now = time.time() if now is None else now
    if proxies is None:
        checked_at = connections_checked_at = connections = None
    checked_at = now if checked_at is None and proxies is not None else checked_at
    if connections is not None and connections_checked_at is None:
        connections_checked_at = checked_at
    configured = mode(line)
    sample = {"status": "unavailable", "target": 0, "foreign": 0, "unknown": 0,
              "unclassified": 0, "identified": 0,
              "checked_at": connections_checked_at if connections is not None else None}
    result = {"configured_mode": configured, "effective_mode": None,
              "locked_node": line.get("locked_node"), "policy_status": "disabled",
              "reason": "AI 专线未启用", "legacy": "exit_policy" not in line,
              "label": LABELS[configured], "boundary_verified": False, "rules_status": "unverified",
              "checked_at": checked_at, "expires_at": checked_at + EVIDENCE_MAX_AGE if checked_at is not None else None,
              "max_age_seconds": EVIDENCE_MAX_AGE, "severity": "warning", "connections": sample,
              "coverage_note": COVERAGE_NOTE, "stale_summary": STALE_SUMMARY,
              "unavailable_summary": UNAVAILABLE_SUMMARY, "inherited_connections": 0}
    if not line.get("enabled"):
        result["severity"] = "info"
    elif proxies is None:
        result.update(policy_status="controller_offline", reason="控制器离线或核验抓取失败，规则与连接路径未核实")
    elif configured != "locked":
        result.update(effective_mode=configured if rules_ok else None,
                      policy_status="active" if rules_ok else "unverified",
                      reason="可能改变出口IP，存在账号风控风险")
    else:
        group = proxies.get(line.get("group_name")) or {}
        target = line.get("locked_node")
        leaf = proxies.get(target) or {}
        present = bool(leaf.get("type")) and "all" not in leaf
        expected = [target] if present else ["REJECT"]
        if connections is not None:
            sample["status"] = "observed"
            for connection in connections:
                if managed_connection(connection, line.get("group_name"), managed_rules):
                    sample["identified"] += 1
                    sample[path_kind(connection, target, proxies)] += 1
                else:
                    sample["unclassified"] += 1
            result["inherited_connections"] = sample["foreign"]
        if not rules_ok or group.get("type") != "Selector" or group.get("all") != expected:
            result.update(policy_status="boundary_unverified", reason="规则约束未核实；请检查配置与 AI 分流规则")
        elif group.get("now") != expected[0]:
            result.update(policy_status="selection_drift", reason="外部选择漂移；未更新锁定目标")
        else:
            result.update(boundary_verified=True, rules_status="verified")
            if connections is None:
                result.update(policy_status="connections_unavailable", reason="规则约束已核实；连接抓取失败或尚无可用快照，路径未核实")
            elif sample["foreign"]:
                sample["status"] = "foreign"
                result.update(policy_status="inherited_sessions", reason="规则约束已核实；%d 条已识别 AI 连接在其他出口，%d 条路径未知；保留会话等待结束" % (sample["foreign"], sample["unknown"]))
            elif sample["unknown"]:
                sample["status"] = "unknown"
                result.update(policy_status="path_unknown", reason="规则约束已核实；%d 条已识别 AI 连接路径未知，无法核实一致性" % sample["unknown"])
            elif not sample["identified"]:
                sample["status"] = "no_sessions"
                result.update(policy_status="no_sessions", severity="info", reason="规则约束已核实；暂无可识别 AI 会话证据")
            elif not present:
                result.update(policy_status="missing_reject", reason="锁定节点消失；规则为拒绝连接，现有路径仍需核实")
            else:
                sample["status"] = "consistent"
                result.update(effective_mode="locked", policy_status="locked", severity="ok",
                              reason="规则约束已核实；已识别 AI 连接 %d/%d 与目标一致" % (sample["target"], sample["identified"]))
            if not present:
                result.update(effective_mode=None, policy_status="missing_reject", severity="warning", reason="锁定节点消失；受管规则指向拒绝，现有连接证据见分类")
            elif probe_ok is False:
                result.update(effective_mode=None, policy_status="probe_abnormal", severity="warning", reason=result["reason"] + "；锁定节点探测异常，保持目标与健康会话")
    result["summary"] = result["reason"]
    result["headline"] = {
        "locked": "规则约束已核实 · 已识别 AI 连接一致", "no_sessions": "规则约束已核实 · 暂无会话证据",
        "inherited_sessions": "存在其他出口的 AI 连接", "path_unknown": "已识别 AI 连接路径未知",
        "connections_unavailable": "规则约束已核实 · 连接证据不可用",
    }.get(result["policy_status"], result["reason"])
    result["connection_summary"] = ("已识别 %d 条：目标一致 %d，其他出口 %d，路径未知 %d；未识别归属 %d 条" %
        (sample["identified"], sample["target"], sample["foreign"], sample["unknown"], sample["unclassified"])) if connections is not None else "连接快照不可用，不能当作零活动连接"
    def stamp(at):
        if at is None:
            return "无有效快照"
        zone = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.fromtimestamp(at, zone).strftime("%Y-%m-%d %H:%M:%S +08:00")
    result["evidence_detail"] = "%s。规则检查：%s；连接快照：%s。%s" % (
        result["connection_summary"], stamp(checked_at), stamp(sample["checked_at"]), COVERAGE_NOTE)
    if connections is not None and connections_checked_at is not None and result["expires_at"] is not None:
        result["expires_at"] = min(result["expires_at"], connections_checked_at + EVIDENCE_MAX_AGE)
    return age_status(result, now)
