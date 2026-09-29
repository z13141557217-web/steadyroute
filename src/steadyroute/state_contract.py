"""Versioned SteadyRoute state, decision copy, and transition events."""

import copy
import hashlib
import json
import os
import pathlib
import tempfile
import time


STATE_SCHEMA_VERSION = 2
API_SCHEMA_VERSION = 2
EVENT_LIMIT = 200

# handover_pending is also reachable from the post-switch states: after a failover the group
# runs in recovery mode, where a confirmed better node may be handed over before the
# performance cooldown or the observation window ends ("lossless return", v0.4.4 fix).
GROUP_TRANSITIONS = {
    "stable": {"candidate_confirming", "degraded"},
    "candidate_confirming": {"stable", "handover_pending", "degraded"},
    "handover_pending": {"stable", "handover_grace", "degraded"},
    "handover_grace": {"recovery_observing", "degraded", "handover_pending"},
    "recovery_observing": {"stable", "degraded", "cooldown", "handover_pending"},
    "cooldown": {"stable", "degraded", "handover_pending"},
    "manual_hold": {"stable", "degraded"},
    "degraded": {"stable", "failover_now", "handover_pending"},
    "failover_now": {"recovery_observing", "no_candidate", "handover_pending"},
    "no_candidate": {"recovery_observing", "stable"},
    "controller_offline": {"recovery_observing", "stable"},
}
GROUP_EXCEPTION_STATES = {"controller_offline", "no_candidate", "manual_hold"}
NODE_TRANSITIONS = {
    "discovered": {"warming"},
    "warming": {"healthy", "degraded"},
    "healthy": {"degraded", "quarantined"},
    "degraded": {"healthy", "quarantined"},
    "quarantined": {"half_open"},
    "half_open": {"healthy", "quarantined"},
    "retired": set(),
}


class StateContractError(ValueError):
    """Base class for state contract failures."""


class FutureStateVersionError(StateContractError):
    """Raised when an older process sees a state version it cannot safely write."""


class InvalidTransitionError(StateContractError):
    """Raised when a caller attempts to skip the declared state machine."""


GROUP_DECISION_COPY = {
    "stable": {
        "severity": "ok", "title": "当前线路稳定且为最佳选择",
        "description": "当前线路健康，暂无已确认的更优候选。",
        "next_action_code": "keep_observing", "next_action": "继续按当前线路频率检测。",
    },
    "candidate_confirming": {
        "severity": "info", "title": "发现更优线路，正在确认稳定性",
        "description": "候选线路已达到改善门槛，但仍需连续确认。",
        "next_action_code": "continue_confirmation", "next_action": "继续确认候选线路，未达门槛前不切换。",
    },
    "handover_pending": {
        "severity": "info", "title": "当前线路已不是最佳，等待安全回优",
        "description": "候选线路已完成确认，后端将按会话粘滞策略安全回优。",
        "next_action_code": "start_safe_handover", "next_action": "让新请求使用目标线路并保留健康旧连接。",
    },
    "handover_grace": {
        "severity": "info", "title": "新请求已回优，旧连接自然结束中",
        "description": "目标线路已接收新请求，健康旧连接仍在宽限期内。",
        "next_action_code": "preserve_old_sessions", "next_action": "等待旧连接自然结束，不主动清理。",
    },
    "recovery_observing": {
        "severity": "info", "title": "切换完成，正在观察恢复质量",
        "description": "切换动作已完成，短期窗口正在验证新线路质量。",
        "next_action_code": "observe_recovery", "next_action": "保持高频检测直至恢复窗口稳定。",
    },
    "cooldown": {
        "severity": "info", "title": "刚完成切换，处于性能冷却期",
        "description": "性能切换冷却尚未结束，暂不再次回优。",
        "next_action_code": "wait_cooldown", "next_action": "等待冷却结束后重新比较候选。",
    },
    "manual_hold": {
        "severity": "info", "title": "手动选择保护，故障保护仍启用",
        "description": "仅暂停性能回优；健康检测、隔离和真实故障切换继续运行。",
        "next_action_code": "respect_manual_choice", "next_action": "保持手动选择，到期后恢复性能比较；真实故障立即安全备援。",
    },
    "degraded": {
        "severity": "warning", "title": "当前线路质量下降",
        "description": "当前线路仍可用，但短期或长期质量已下降。",
        "next_action_code": "expand_health_checks", "next_action": "提高当前线路检测频率并观察安全候选。",
    },
    "failover_now": {
        "severity": "critical", "title": "当前线路故障，立即切换",
        "description": "基础或业务可达性已确认真实故障，必须立即故障切换。",
        "next_action_code": "switch_and_close_failed", "next_action": "切到健康候选并只清理失效线路连接。",
    },
    "no_candidate": {
        "severity": "critical", "title": "没有可安全使用的候选线路",
        "description": "当前没有满足安全要求的候选，系统不会静默回落 DIRECT。",
        "next_action_code": "fail_closed", "next_action": "保持 fail-closed 并继续低频寻找安全候选。",
    },
    "controller_offline": {
        "severity": "critical", "title": "控制器连接中断",
        "description": "SteadyRoute 当前无法读取或操作 Mihomo 控制器。",
        "next_action_code": "reconnect_controller", "next_action": "保留最后快照并等待控制器恢复连接。",
    },
}


NODE_LIFECYCLE_COPY = {
    "discovered": {
        "severity": "info", "title": "新节点已发现", "description": "节点刚进入候选集合，尚无健康样本。",
        "next_action_code": "begin_warmup", "next_action": "开始低风险预热检测。",
    },
    "warming": {
        "severity": "info", "title": "节点正在预热", "description": "节点样本尚未达到参与性能回优的门槛。",
        "next_action_code": "collect_samples", "next_action": "继续收集基础与业务健康样本。",
    },
    "healthy": {
        "severity": "ok", "title": "节点健康", "description": "节点通过分层健康判断，可作为安全候选。",
        "next_action_code": "keep_sampling", "next_action": "按当前或备用频率继续检测。",
    },
    "degraded": {
        "severity": "warning", "title": "节点质量下降", "description": "节点仍有响应，但近期健康指标已经下降。",
        "next_action_code": "watch_degradation", "next_action": "提高关注度并避免未经确认的回优。",
    },
    "quarantined": {
        "severity": "critical", "title": "节点已隔离", "description": "节点在故障窗口内达到隔离门槛。",
        "next_action_code": "wait_quarantine", "next_action": "隔离期内不参与选路并保留低频验证。",
    },
    "half_open": {
        "severity": "warning", "title": "节点正在半开放验证", "description": "隔离时间已到，节点正在用有限探测验证恢复。",
        "next_action_code": "verify_recovery", "next_action": "达到连续成功门槛后恢复健康状态。",
    },
    "retired": {
        "severity": "info", "title": "节点已退役", "description": "节点已从订阅候选集合消失并进入审计保留期。",
        "next_action_code": "retain_audit_record", "next_action": "保留二十四小时审计状态后再清理。",
    },
}


def new_state():
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "nodes": {},
        "groups": {},
        "events": [],
        "subscription": {
            "generation": 0,
            "candidate_count": 0,
            "added_count": 0,
            "removed_count": 0,
            "changes": [],
        },
    }


def _validate_state_shape(state):
    if not isinstance(state, dict):
        raise StateContractError("state must be an object")
    for name in ("nodes", "groups"):
        if not isinstance(state.get(name, {}), dict):
            raise StateContractError("%s must be an object" % name)
    if not isinstance(state.get("events", []), list):
        raise StateContractError("events must be an array")
    if not isinstance(state.get("subscription", {}), dict):
        raise StateContractError("subscription must be an object")


def migrate_state(source):
    if not isinstance(source, dict):
        raise StateContractError("state must be an object")
    version = source.get("schema_version", source.get("version", 1))
    if not isinstance(version, int) or isinstance(version, bool):
        raise StateContractError("state schema version must be an integer")
    if version > STATE_SCHEMA_VERSION:
        raise FutureStateVersionError("unsupported future state schema %d" % version)
    if version < 1:
        raise StateContractError("unsupported state schema %d" % version)
    migrated = copy.deepcopy(source)
    if version == 1:
        _validate_state_shape(migrated)
        migrated.pop("version", None)
        migrated["schema_version"] = 2
        migrated.setdefault("events", [])
        migrated.setdefault("subscription", {
            "generation": 0, "candidate_count": 0, "added_count": 0,
            "removed_count": 0, "changes": [],
        })
    _validate_state_shape(migrated)
    migrated.setdefault("events", [])
    migrated.setdefault("subscription", {
        "generation": 0, "candidate_count": 0, "added_count": 0,
        "removed_count": 0, "changes": [],
    })
    if migrated.get("schema_version") != STATE_SCHEMA_VERSION:
        raise StateContractError("migration did not reach current schema")
    return migrated


def _atomic_write(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _encoded_state(state):
    return (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def save_persistent_state(path, state):
    migrated = migrate_state(state)
    _atomic_write(path, _encoded_state(migrated))


def _diagnostic_path(path, label, now):
    path = pathlib.Path(path)
    return path.with_name("%s.%s-%d%s" % (path.stem, label, int(now), path.suffix))


def load_persistent_state(path, now=None):
    path = pathlib.Path(path)
    if not path.exists():
        return new_state()
    if now is None:
        now = int(time.time())
    raw = path.read_bytes()
    try:
        source = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _atomic_write(_diagnostic_path(path, "corrupt", now), raw)
        rebuilt = new_state()
        save_persistent_state(path, rebuilt)
        return rebuilt

    version = source.get("schema_version", source.get("version", 1)) if isinstance(source, dict) else 1
    if isinstance(version, int) and not isinstance(version, bool) and version > STATE_SCHEMA_VERSION:
        raise FutureStateVersionError("unsupported future state schema %d" % version)
    try:
        migrated = migrate_state(source)
    except FutureStateVersionError:
        raise
    except StateContractError:
        _atomic_write(_diagnostic_path(path, "migration-failed", now), raw)
        rebuilt = new_state()
        save_persistent_state(path, rebuilt)
        return rebuilt
    if version == 1:
        backup = path.with_name("%s.v1-backup%s" % (path.stem, path.suffix))
        _atomic_write(backup, raw)
        save_persistent_state(path, migrated)
    return migrated


def node_ui_id(group_name, node_name):
    digest = hashlib.sha256((group_name + "\0" + node_name).encode("utf-8")).hexdigest()[:16]
    return "node-" + digest


def group_ui_id(group_name):
    digest = hashlib.sha256(group_name.encode("utf-8")).hexdigest()[:16]
    return "group-" + digest


def utc_iso(value):
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(value)))


def transition_allowed(scope, old_state, new_state):
    if scope == "group":
        known = set(GROUP_TRANSITIONS)
    elif scope == "node":
        known = set(NODE_TRANSITIONS)
    else:
        return False
    if old_state not in known or new_state not in known:
        return False
    if old_state == new_state:
        return True
    if scope == "group":
        return new_state in GROUP_EXCEPTION_STATES or new_state in GROUP_TRANSITIONS.get(old_state, set())
    if scope == "node":
        return new_state == "retired" or new_state in NODE_TRANSITIONS.get(old_state, set())
    return False


def transition_path(scope, old_state, new_state):
    """Return declared states after old_state through new_state, or reject the jump."""
    if scope == "group":
        known = set(GROUP_TRANSITIONS)
        if old_state not in known or new_state not in known:
            raise InvalidTransitionError("unknown group state in transition")
        if new_state in GROUP_EXCEPTION_STATES:
            return [new_state]
        transitions = GROUP_TRANSITIONS
    elif scope == "node":
        known = set(NODE_TRANSITIONS)
        if old_state not in known or new_state not in known:
            raise InvalidTransitionError("unknown node state in transition")
        if new_state == "retired":
            return [new_state]
        transitions = NODE_TRANSITIONS
    else:
        raise InvalidTransitionError("unknown transition scope: %s" % scope)
    if old_state == new_state:
        return []
    queue = [(old_state, [])]
    visited = {old_state}
    while queue:
        current, path = queue.pop(0)
        neighbors = set(transitions.get(current, set()))
        for candidate in sorted(neighbors):
            if candidate == new_state:
                return path + [candidate]
            if candidate not in visited:
                visited.add(candidate)
                queue.append((candidate, path + [candidate]))
    raise InvalidTransitionError(
        "undeclared %s transition path: %s -> %s" % (scope, old_state, new_state)
    )


def resolve_group_decision(facts, updated_at):
    required = max(1, int(facts.get("confirmation_required", 3)))
    current = max(0, int(facts.get("confirmation_current", 0)))
    if not facts.get("controller_connected", False):
        code, reason = "controller_offline", "controller_unreachable"
    elif int(facts.get("candidate_count", 0)) <= 0:
        code, reason = "no_candidate", "safe_candidate_unavailable"
    elif facts.get("current_failed") and facts.get("target_id"):
        code, reason = "failover_now", "confirmed_current_failure"
    elif facts.get("current_failed"):
        code, reason = "no_candidate", "failed_without_safe_candidate"
    elif int(facts.get("manual_hold_remaining_seconds", 0)) > 0:
        code, reason = "manual_hold", "manual_selection_active"
    elif facts.get("handover_active"):
        code, reason = "handover_grace", "healthy_old_connections_present"
    elif facts.get("recovery_observing"):
        code, reason = "recovery_observing", "post_switch_validation"
    elif facts.get("current_degraded"):
        code, reason = "degraded", "health_window_degraded"
    elif int(facts.get("cooldown_remaining_seconds", 0)) > 0:
        code, reason = "cooldown", "performance_cooldown_active"
    elif facts.get("handover_pending") or (facts.get("target_id") and current >= required):
        code, reason = "handover_pending", "confirmation_complete"
    elif facts.get("target_id") and current > 0:
        code, reason = "candidate_confirming", "confirmation_incomplete"
    else:
        code, reason = "stable", "current_best"
    copy_item = GROUP_DECISION_COPY[code]
    detail = copy_item["description"]
    if code == "candidate_confirming":
        detail = "%s 当前为 %d/%d。" % (detail.rstrip("。"), current, required)
    title = copy_item["title"]
    if code == "manual_hold":
        remaining = max(0, int(facts.get("manual_hold_remaining_seconds", 0)))
        if facts.get("safe_backup_available"):
            title = "手动选择保护（剩余 %02d:%02d），故障时将预检成熟备援" % divmod(remaining, 60)
        else:
            title = "手动选择保护（剩余 %02d:%02d），暂无成熟备援" % divmod(remaining, 60)
            detail = "故障保护仍启用；当前没有成熟安全备援，故障时将 fail-closed 并继续检测。"
    return {
        "code": code,
        "severity": copy_item["severity"],
        "title": title,
        "detail": detail,
        "reason_code": reason,
        "updated_at": int(updated_at),
        "updated_at_iso": utc_iso(updated_at),
        "next_action_code": copy_item["next_action_code"],
        "next_action": copy_item["next_action"],
    }


def resolve_node_lifecycle(facts, now):
    quarantine_until = facts.get("quarantine_until")
    if facts.get("retired_at") is not None:
        code = "retired"
    elif quarantine_until is not None and int(quarantine_until) > int(now):
        code = "quarantined"
    elif quarantine_until is not None and int(quarantine_until) > 0 and int(facts.get("quarantine_recovery_streak", 0)) < 3:
        code = "half_open"
    elif int(facts.get("samples", 0)) == 0:
        code = "discovered"
    elif int(facts.get("samples", 0)) < 10:
        code = "warming"
    elif int(facts.get("failure_streak", 0)) > 0 or not facts.get("last_success", False):
        code = "degraded"
    else:
        code = "healthy"
    copy_item = NODE_LIFECYCLE_COPY[code]
    return {
        "code": code,
        "severity": copy_item["severity"],
        "title": copy_item["title"],
        "detail": copy_item["description"],
        "reason_code": "%s_criteria" % code,
        "next_action_code": copy_item["next_action_code"],
        "next_action": copy_item["next_action"],
    }


def record_transition(state, scope, subject_id, old_state, new_state, reason_code, occurred_at, group_id=None):
    if not transition_allowed(scope, old_state, new_state):
        raise InvalidTransitionError(
            "undeclared %s transition: %s -> %s" % (scope, old_state, new_state)
        )
    if old_state == new_state:
        return False
    event = {
        "code": "%s_state_changed" % scope,
        "severity": "info",
        "scope": scope,
        "subject_id": subject_id,
        "group_id": subject_id if scope == "group" else group_id,
        "node_id": subject_id if scope == "node" else None,
        "from_state": old_state,
        "to_state": new_state,
        "reason_code": reason_code,
        "occurred_at": int(occurred_at),
        "occurred_at_iso": utc_iso(occurred_at),
    }
    events = list(state.get("events", []))
    events.append(event)
    state["events"] = events[-EVENT_LIMIT:]
    return True


def record_unmodelled_transition(state, scope, subject_id, old_state, new_state, occurred_at, group_id=None):
    """Fallback for a real change the transition table does not model; never raises."""
    event = {
        "code": "%s_state_changed" % scope,
        "severity": "warning",
        "scope": scope,
        "subject_id": subject_id,
        "group_id": subject_id if scope == "group" else group_id,
        "node_id": subject_id if scope == "node" else None,
        "from_state": old_state,
        "to_state": new_state,
        "reason_code": "unmodelled_transition",
        "occurred_at": int(occurred_at),
        "occurred_at_iso": utc_iso(occurred_at),
    }
    events = list(state.get("events", []))
    events.append(event)
    state["events"] = events[-EVENT_LIMIT:]
    return True


def record_transition_path(
        state, scope, subject_id, old_state, final_state, steps, occurred_at, group_id=None):
    """Record an explicit ordered path; every individual edge must be declared."""
    if old_state == final_state and not steps:
        return False
    if not steps or steps[-1][0] != final_state:
        raise InvalidTransitionError("explicit path must end at %s" % final_state)
    changed = False
    current = old_state
    for next_state, reason_code in steps:
        changed = record_transition(
            state, scope, subject_id, current, next_state, reason_code,
            occurred_at, group_id=group_id,
        ) or changed
        current = next_state
    return changed
