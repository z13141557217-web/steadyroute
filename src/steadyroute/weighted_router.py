#!/usr/bin/python3
"""Weighted, connection-aware selector for Clash Verge residential routes."""

import argparse
import concurrent.futures
import fcntl
import http.server
import json
import os
import resource
import socket
import sys
import threading
import time
from urllib.parse import quote, urlsplit

try:
    import state_contract
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import state_contract


SOCKET_PATH = "/tmp/verge/verge-mihomo.sock"
BASE_DIR = "/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router"
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOCK_PATH = os.path.join(BASE_DIR, "router.lock")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ACCEPTANCE_DASHBOARD_PATH = os.path.join(APP_DIR, "acceptance_dashboard.html")
ACCEPTANCE_FIXTURE_PATH = os.path.join(APP_DIR, "fixtures", "status_contract_v2.json")
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 17654
SERVICE_NAME = "稳航 SteadyRoute"
SERVICE_STARTED_AT = int(time.time())
DASHBOARD_CACHE = None
DASHBOARD_CACHE_LOCK = threading.Lock()
TEST_URL = "https://cp.cloudflare.com/generate_204"
PROBE_TIMEOUT_MS = 5000
BUSINESS_PROBE_TIMEOUT_MS = 8000
PROBE_INTERVAL_SECONDS = 20
BUSINESS_PROBE_INTERVAL_SECONDS = 60
STANDBY_PROBES_PER_GROUP = 1
FAILURES_BEFORE_SWITCH = 2
MIN_SAMPLES_FOR_OPTIMIZATION = 10
MIN_SUCCESS_STREAK = 3
MIN_AVAILABILITY = 0.90
PERFORMANCE_CONFIRMATIONS = 3
MIN_ABSOLUTE_GAIN_MS = 180
MIN_RELATIVE_GAIN = 0.30
PERFORMANCE_COOLDOWN_SECONDS = 30 * 60
MANUAL_HOLD_SECONDS = 6 * 60 * 60
LATENCY_ALPHA = 0.25
AVAILABILITY_ALPHA = 0.20
JITTER_ALPHA = 0.25
AVAILABILITY_PENALTY_MS = 4000
JITTER_WEIGHT = 1.5
SHORT_WINDOW_SIZE = 20
LONG_WINDOW_HOURS = 24
SHORT_MIN_AVAILABILITY = 0.80
QUARANTINE_FAILURES = 3
QUARANTINE_WINDOW_SECONDS = 10 * 60
QUARANTINE_SECONDS = 30 * 60
QUARANTINE_RECOVERY_SUCCESSES = 3

GROUPS = {
    "香港家宽自动备援": [
        "香港家宽hy2🇭🇰",
        "优秀|【3x】中转|香港家宽🇭🇰",
        "优秀|cf加速|香港动态家宽🇭🇰",
        "优秀|cf加速|香港动态家宽二🇭🇰",
        "【10x】三网优化|香港动态家宽🇭🇰",
    ],
    "AI 台湾家宽线路": [
        "[03]台湾hinet家宽🇨🇳hy2",
        "[01]台湾hinet家宽🇨🇳hy2",
        "[02]台湾hinet家宽🇨🇳hy2",
        "【10x】三网优化|台湾hinet家宽02",
        "【10x】三网优化|台湾hinet动态家宽01",
        "优秀|[3x]中转|台湾hinet家宽02",
        "优秀|[3x]中转|台湾hinet家宽03",
        "优秀|【3x】中转|台湾hinet动态家宽01",
        "【3x】中转|台湾seednet动态家宽🇹🇼",
        "优秀|cf加速|台湾动态家宽🇹🇼",
        "台湾seednet动态家宽🇹🇼hy2",
    ],
}

BUSINESS_TEST_URLS = {
    "香港家宽自动备援": ["https://grok.com/cdn-cgi/trace"],
    "AI 台湾家宽线路": [
        "https://chatgpt.com/cdn-cgi/trace",
        "https://claude.ai/cdn-cgi/trace",
    ],
}


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


def decode_chunked(data):
    output = bytearray()
    cursor = 0
    while cursor < len(data):
        line_end = data.find(b"\r\n", cursor)
        if line_end < 0:
            break
        size_text = data[cursor:line_end].split(b";", 1)[0]
        size = int(size_text, 16)
        if size == 0:
            break
        cursor = line_end + 2
        output.extend(data[cursor:cursor + size])
        cursor += size + 2
    return bytes(output)


def api_request(method, path, payload=None, timeout=10):
    body = b""
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = [
        "%s %s HTTP/1.1" % (method, path),
        "Host: localhost",
        "Accept: application/json",
        "Connection: close",
    ]
    if body:
        request.extend([
            "Content-Type: application/json",
            "Content-Length: %d" % len(body),
        ])
    raw_request = ("\r\n".join(request) + "\r\n\r\n").encode("ascii") + body

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(SOCKET_PATH)
        client.sendall(raw_request)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        client.close()

    response = b"".join(chunks)
    header_blob, separator, response_body = response.partition(b"\r\n\r\n")
    if not separator:
        raise RuntimeError("invalid controller response")
    header_lines = header_blob.split(b"\r\n")
    status = int(header_lines[0].split()[1])
    headers = {}
    for line in header_lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.strip().lower()] = value.strip().lower()
    if headers.get(b"transfer-encoding") == b"chunked":
        response_body = decode_chunked(response_body)
    if status >= 400:
        raise RuntimeError("controller HTTP %d: %s" % (status, response_body[:200]))
    if not response_body:
        return None
    return json.loads(response_body.decode("utf-8"))


def load_state():
    return state_contract.load_persistent_state(STATE_PATH)


def save_state(state):
    state_contract.save_persistent_state(STATE_PATH, state)


def memory_megabytes():
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return round(float(usage) / 1024.0 / 1024.0, 1)
    return round(float(usage) / 1024.0, 1)


def quality_score(node_state):
    """Return an intuitive 0-100 quality score; higher is better."""
    availability = min(1.0, max(0.0, float(node_state.get("availability_ewma", 0.0))))
    latency = node_state.get("latency_ewma")
    if latency is None:
        return 0
    jitter = max(0.0, float(node_state.get("jitter_ewma", 0.0)))
    latency_quality = 100.0 / (1.0 + max(0.0, float(latency)) / 300.0)
    jitter_quality = 100.0 / (1.0 + jitter / 100.0)
    weighted = 65.0 * availability * availability + 0.25 * latency_quality + 0.10 * jitter_quality
    confidence = 0.70 + 0.30 * min(1.0, float(node_state.get("samples", 0)) / MIN_SAMPLES_FOR_OPTIMIZATION)
    quality = weighted * confidence
    if not node_state.get("last_success"):
        quality = min(quality, 39.0)
    quality -= min(24.0, 8.0 * float(node_state.get("failure_streak", 0)))
    return int(round(min(100.0, max(0.0, quality))))


def iso_timestamp(value):
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(value)))


def read_service_version():
    candidates = [
        os.path.join(APP_DIR, "VERSION"),
        os.path.join(os.path.dirname(APP_DIR), "VERSION"),
        os.path.join(os.path.dirname(os.path.dirname(APP_DIR)), "VERSION"),
        os.path.join(BASE_DIR, "VERSION"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            continue
    return "unknown"


def region_for_group(group_name):
    return "TW" if "台湾" in group_name else "HK" if "香港" in group_name else "unknown"


def connection_count_for_node(group_name, node_name, connections):
    return sum(
        1 for connection in connections
        if group_name in (connection.get("chains") or [])
        and node_name in (connection.get("chains") or [])
    )


def group_decision_facts(group_name, candidates, state, proxy_data, connections, now):
    group_state = state.get("groups", {}).get(group_name, {})
    current = (proxy_data.get(group_name) or {}).get("now") or group_state.get("last_seen")
    current_state = state.get("nodes", {}).get(current, {})
    failures = int(current_state.get("effective_failure_streak", current_state.get("failure_streak", 0)))
    target = group_state.get("better_candidate")
    if not target and int(now) < int(group_state.get("recovery_observe_until", 0)):
        target = group_state.get("handover_new_node")
    last_switch = int(group_state.get("last_switch_at", 0))
    cooldown = max(0, PERFORMANCE_COOLDOWN_SECONDS - (int(now) - last_switch)) if last_switch else 0
    manual_hold = max(0, int(group_state.get("manual_hold_until", 0)) - int(now))
    old_node = group_state.get("handover_old_node")
    old_connections = connection_count_for_node(group_name, old_node, connections) if old_node else 0
    handover_until = int(group_state.get("handover_grace_until", 0))
    recovery_until = int(group_state.get("recovery_observe_until", 0))
    return {
        "controller_connected": bool(state.get("controller_connected", bool(proxy_data))),
        "candidate_count": len(candidates),
        "current_healthy": bool(current_state.get("last_success")),
        "current_degraded": bool(
            current_state.get("last_success")
            and (
                int(current_state.get("failure_streak", 0)) > 0
                or short_availability(current_state) < SHORT_MIN_AVAILABILITY
                or long_availability(current_state) < MIN_AVAILABILITY
            )
        ),
        "current_failed": failures >= FAILURES_BEFORE_SWITCH,
        "target_id": state_contract.node_ui_id(group_name, target) if target else None,
        "confirmation_current": int(group_state.get("better_streak", 0)),
        "confirmation_required": PERFORMANCE_CONFIRMATIONS,
        "handover_pending": bool(group_state.get("handover_pending")),
        "handover_active": bool(old_node and old_connections and int(now) < handover_until),
        "old_connections": old_connections,
        "grace_remaining_seconds": max(0, handover_until - int(now)),
        "recovery_observing": bool(int(now) < recovery_until),
        "cooldown_remaining_seconds": cooldown,
        "manual_hold_remaining_seconds": manual_hold,
    }


def node_contract(group_name, node_name, node_state, now, current_names):
    lifecycle = state_contract.resolve_node_lifecycle(node_state, now)
    latency = node_state.get("latency_ewma")
    score = node_state.get("score")
    samples = int(node_state.get("samples", 0))
    quarantine_until = node_state.get("quarantine_until")
    if not quarantine_until:
        quarantine_until = None
    last_probe = node_state.get("last_probe_at")
    current = node_name in current_names
    probe_gap = PROBE_INTERVAL_SECONDS if current else PROBE_INTERVAL_SECONDS * max(1, len(GROUPS.get(group_name, [])))
    return {
        "id": state_contract.node_ui_id(group_name, node_name),
        "name": node_name,
        "group": group_name,
        "region": region_for_group(group_name),
        "lifecycle": lifecycle["code"],
        "lifecycle_status": lifecycle,
        "role": "current" if current else "candidate",
        "transport": "unknown",
        "availability_short": round(short_availability(node_state), 4) if samples else None,
        "availability_long": round(long_availability(node_state), 4) if samples else None,
        "latency_ewma_ms": round(float(latency), 1) if latency is not None else None,
        "latency_p95_ms": node_state.get("latency_p95_ms"),
        "jitter_ms": round(float(node_state.get("jitter_ewma", 0.0)), 1) if latency is not None else None,
        "score": round(float(score), 1) if score is not None else None,
        "lower_score_is_better": True,
        "quality": quality_score(node_state) if latency is not None else None,
        "success_streak": int(node_state.get("success_streak", 0)),
        "failure_streak": int(node_state.get("failure_streak", 0)),
        "sample_count_short": len(node_state.get("short_results", [])),
        "sample_count_long": sum(int(item.get("total", 0)) for item in node_state.get("long_buckets", [])),
        "last_probe_at": int(last_probe) if last_probe is not None else None,
        "last_probe_at_iso": iso_timestamp(last_probe),
        "next_probe_at": int(last_probe) + probe_gap if last_probe is not None else None,
        "next_probe_at_iso": iso_timestamp(int(last_probe) + probe_gap) if last_probe is not None else None,
        "quarantine_until": int(quarantine_until) if quarantine_until is not None else None,
        "quarantine_until_iso": iso_timestamp(quarantine_until),
        "warmup_progress": round(min(1.0, float(samples) / MIN_SAMPLES_FOR_OPTIMIZATION), 3)
        if lifecycle["code"] == "warming" else None,
    }


def public_event(event):
    fields = (
        "code", "severity", "scope", "subject_id", "group_id", "node_id",
        "from_state", "to_state", "reason_code", "occurred_at", "occurred_at_iso",
    )
    return {name: event.get(name) for name in fields}


def public_subscription_change(change):
    fields = ("code", "node_id", "group_id", "from_state", "to_state", "occurred_at")
    public = {name: change.get(name) for name in fields if name in change}
    if "occurred_at" in public:
        public["occurred_at_iso"] = iso_timestamp(public["occurred_at"])
    return public


def transition_reason(scope, state_code, final_decision=None):
    if final_decision is not None and state_code == final_decision["code"]:
        return final_decision["reason_code"]
    if scope == "group":
        return {
            "stable": "current_best",
            "candidate_confirming": "confirmation_incomplete",
            "handover_pending": "confirmation_complete",
            "handover_grace": "safe_handover_started",
            "recovery_observing": "post_switch_validation",
            "cooldown": "performance_cooldown_active",
            "manual_hold": "manual_selection_active",
            "degraded": "health_window_degraded",
            "failover_now": "confirmed_current_failure",
            "no_candidate": "safe_candidate_unavailable",
            "controller_offline": "controller_unreachable",
        }[state_code]
    return "%s_criteria" % state_code


def record_projected_transition(
        state, scope, subject_id, old_state, new_state, occurred_at,
        final_decision=None, group_id=None, explicit_steps=None):
    if old_state == new_state:
        return False
    steps = []
    for code, reason_code in explicit_steps or []:
        if code != old_state and (not steps or steps[-1][0] != code):
            steps.append((code, reason_code))
    cursor = steps[-1][0] if steps else old_state
    for code in state_contract.transition_path(scope, cursor, new_state):
        if not steps or steps[-1][0] != code:
            steps.append((code, transition_reason(scope, code, final_decision=final_decision)))
    return state_contract.record_transition_path(
        state, scope, subject_id, old_state, new_state, steps, occurred_at,
        group_id=group_id,
    )


def build_status_snapshots(state, proxy_data, connections, now=None, memory_mb=None):
    now = int(time.time()) if now is None else int(now)
    memory_mb = memory_megabytes() if memory_mb is None else float(memory_mb)
    migrated = state_contract.migrate_state(state)
    state.clear()
    state.update(migrated)
    last_cycle = state.get("updated_at")
    stale_after = PROBE_INTERVAL_SECONDS * 3
    state_stale = last_cycle is None or now - int(last_cycle) > stale_after
    resumed = bool(state.get("last_resume_at") and now - int(state.get("last_resume_at")) < 180)
    if state_stale:
        service_state = {
            "code": "stale", "severity": "warning", "title": "检测暂停",
            "detail": "最后成功周期已超过新鲜度门槛。", "next_action": "等待下一次成功检测周期。",
        }
    elif resumed:
        service_state = {
            "code": "resume_recovery", "severity": "warning", "title": "刚从休眠恢复",
            "detail": "连接正在按需重建，后端保持高频观察。", "next_action": "等待短期健康窗口重新稳定。",
        }
    else:
        service_state = {
            "code": "running", "severity": "ok", "title": "运行中",
            "detail": "最近检测周期有效，缓存快照保持新鲜。", "next_action": "继续按既定策略检测。",
        }
    current_names = {
        (proxy_data.get(group_name) or {}).get("now") or state.get("groups", {}).get(group_name, {}).get("last_seen")
        for group_name in GROUPS
    }
    current_names.discard(None)

    nodes = []
    node_by_name = {}
    for node_name, node_state in state.get("nodes", {}).items():
        group_name = next((name for name, candidates in GROUPS.items() if node_name in candidates), "unknown")
        item = node_contract(group_name, node_name, node_state, now, current_names)
        nodes.append(item)
        node_by_name[node_name] = item
        previous = node_state.get("lifecycle")
        node_state["lifecycle"] = item["lifecycle"]
        if previous is not None:
            record_projected_transition(
                state, "node", item["id"], previous, item["lifecycle"], now,
                group_id=state_contract.group_ui_id(group_name),
            )

    groups = []
    for group_name, candidates in GROUPS.items():
        group_state = state.get("groups", {}).setdefault(group_name, {})
        current_name = (proxy_data.get(group_name) or {}).get("now") or group_state.get("last_seen")
        target_name = group_state.get("better_candidate")
        if not target_name and now < int(group_state.get("recovery_observe_until", 0)):
            target_name = group_state.get("handover_new_node")
        facts = group_decision_facts(group_name, candidates, state, proxy_data, connections, now)
        decision = state_contract.resolve_group_decision(facts, now)
        previous = group_state.get("decision_code")
        group_state["decision_code"] = decision["code"]
        if previous is not None:
            pending_steps = [
                (item["code"], item["reason_code"])
                for item in group_state.get("pending_decision_events", [])
            ]
            if pending_steps and decision["code"] == "recovery_observing":
                pending_steps.append(("handover_grace", "no_old_connections"))
            record_projected_transition(
                state, "group", state_contract.group_ui_id(group_name), previous,
                decision["code"], now, final_decision=decision,
                explicit_steps=pending_steps,
            )
        group_state["pending_decision_events"] = []
        current = node_by_name.get(current_name)
        target = node_by_name.get(target_name)
        groups.append({
            "id": state_contract.group_ui_id(group_name),
            "name": group_name,
            "region": region_for_group(group_name),
            "decision": decision,
            "current": current,
            "target": target,
            "confirmation": {
                "current": facts["confirmation_current"],
                "required": facts["confirmation_required"],
            },
            "handover": {
                "mode": "session_sticky",
                "old_node": (
                    group_state.get("handover_old_node")
                    if target_name and group_state.get("handover_new_node") == target_name
                    else current_name if target_name else None
                ),
                "new_node": target_name,
                "old_connections": facts["old_connections"],
                "grace_remaining_seconds": facts["grace_remaining_seconds"],
                "max_grace_seconds": 300,
                "new_connections_use_target": bool(
                    target_name and group_state.get("handover_new_node") == target_name
                ),
            },
            "timers": {
                "cooldown_remaining_seconds": facts["cooldown_remaining_seconds"],
                "manual_hold_remaining_seconds": facts["manual_hold_remaining_seconds"],
                "estimated_action_at": (
                    now + facts["grace_remaining_seconds"]
                    if facts["grace_remaining_seconds"] else None
                ),
                "estimated_action_at_iso": (
                    iso_timestamp(now + facts["grace_remaining_seconds"])
                    if facts["grace_remaining_seconds"] else None
                ),
            },
        })

    subscription = state.get("subscription", {})
    refresh_at = subscription.get("last_refresh_at")
    snapshot_id = "snapshot-%d-%d" % (now, int(last_cycle or 0))
    versioned = {
        "schema_version": state_contract.API_SCHEMA_VERSION,
        "generated_at": now,
        "generated_at_iso": iso_timestamp(now),
        "service": {
            "name": SERVICE_NAME,
            "status": "running",
            "state": service_state,
            "started_at": SERVICE_STARTED_AT,
            "started_at_iso": iso_timestamp(SERVICE_STARTED_AT),
            "uptime_seconds": max(0, now - SERVICE_STARTED_AT),
            "controller_connected": bool(state.get("controller_connected", bool(proxy_data))),
            "last_cycle_at": int(last_cycle) if last_cycle is not None else None,
            "last_cycle_at_iso": iso_timestamp(last_cycle),
            "last_cycle_duration_ms": state.get("last_cycle_duration_ms"),
            "next_cycle_at": int(last_cycle) + PROBE_INTERVAL_SECONDS if last_cycle is not None else None,
            "next_cycle_at_iso": iso_timestamp(int(last_cycle) + PROBE_INTERVAL_SECONDS) if last_cycle is not None else None,
            "state_stale": state_stale,
            "stale_after_seconds": stale_after,
            "memory_mb": memory_mb,
            "version": read_service_version(),
        },
        "subscription": {
            "generation": int(subscription.get("generation", 0)),
            "last_refresh_at": int(refresh_at) if refresh_at is not None else None,
            "last_refresh_at_iso": iso_timestamp(refresh_at),
            "candidate_count": int(subscription.get("candidate_count", sum(len(items) for items in GROUPS.values()))),
            "added_count": int(subscription.get("added_count", 0)),
            "removed_count": int(subscription.get("removed_count", 0)),
            "warming_count": sum(1 for node in nodes if node["lifecycle"] == "warming"),
            "quarantined_count": sum(1 for node in nodes if node["lifecycle"] == "quarantined"),
            "changes": [public_subscription_change(item) for item in subscription.get("changes", [])],
        },
        "policies": {
            "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
            "business_probe_interval_seconds": BUSINESS_PROBE_INTERVAL_SECONDS,
            "standby_probes_per_group": STANDBY_PROBES_PER_GROUP,
            "min_samples": MIN_SAMPLES_FOR_OPTIMIZATION,
            "confirmations": PERFORMANCE_CONFIRMATIONS,
            "min_availability": MIN_AVAILABILITY,
            "short_min_availability": SHORT_MIN_AVAILABILITY,
            "short_window_size": SHORT_WINDOW_SIZE,
            "long_window_hours": LONG_WINDOW_HOURS,
            "failures_before_switch": FAILURES_BEFORE_SWITCH,
            "quarantine_failures": QUARANTINE_FAILURES,
            "quarantine_seconds": QUARANTINE_SECONDS,
            "quarantine_recovery_successes": QUARANTINE_RECOVERY_SUCCESSES,
            "performance_cooldown_seconds": PERFORMANCE_COOLDOWN_SECONDS,
            "score": {"lower_is_better": True, "unit": "weighted_milliseconds"},
        },
        "groups": groups,
        "nodes": nodes,
        "events": [public_event(item) for item in state.get("events", [])][-state_contract.EVENT_LIMIT:],
        "history_summary": {
            "short_window_samples": SHORT_WINDOW_SIZE,
            "long_window_hours": LONG_WINDOW_HOURS,
            "event_count": len(state.get("events", [])),
        },
        "diagnostics": {
            "snapshot_id": snapshot_id,
            "cache_only": True,
            "contains_sensitive_fields": False,
        },
    }

    legacy_nodes = {}
    for node in nodes:
        legacy_nodes[node["name"]] = {
            "availability": node["availability_long"],
            "latency": node["latency_ewma_ms"],
            "jitter": node["jitter_ms"],
            "score": node["score"],
            "quality": node["quality"],
            "last_delay": state["nodes"][node["name"]].get("last_delay"),
            "last_success": bool(state["nodes"][node["name"]].get("last_success")),
            "success_streak": node["success_streak"],
            "failure_streak": node["failure_streak"],
            "samples": int(state["nodes"][node["name"]].get("samples", 0)),
            "short_availability": node["availability_short"],
            "long_availability": node["availability_long"],
            "quarantined": node["lifecycle"] == "quarantined",
            "quarantine_until": node["quarantine_until"],
            "business_last_success": state["nodes"][node["name"]].get("business_last_success"),
            "lifecycle": node["lifecycle"],
            "lifecycle_title": node["lifecycle_status"]["title"],
            "lifecycle_severity": node["lifecycle_status"]["severity"],
            "lifecycle_detail": node["lifecycle_status"]["detail"],
        }
    legacy_groups = []
    for group in groups:
        group_state = state.get("groups", {}).get(group["name"], {})
        legacy_groups.append({
            "name": group["name"],
            "region": group["region"],
            "current": group["current"]["name"] if group["current"] else group_state.get("last_seen"),
            "active_connections": sum(
                1 for connection in connections if group["name"] in (connection.get("chains") or [])
            ),
            "decision": group["decision"]["title"],
            "decision_code": group["decision"]["code"],
            "decision_severity": group["decision"]["severity"],
            "decision_detail": group["decision"]["detail"],
            "next_action": group["decision"]["next_action"],
            "better_candidate": group["target"]["name"] if group["target"] else None,
            "better_streak": group["confirmation"]["current"],
            "manual_hold_until": int(group_state.get("manual_hold_until", 0)),
            "last_switch_at": int(group_state.get("last_switch_at", 0)),
            "candidates": list(GROUPS[group["name"]]),
        })
    legacy = {
        "snapshot_id": snapshot_id,
        "service": {
            "name": SERVICE_NAME,
            "status": "running",
            "started_at": SERVICE_STARTED_AT,
            "updated_at": int(last_cycle) if last_cycle is not None else None,
            "memory_mb": memory_mb,
            "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
            "last_cycle_gap_seconds": int(state.get("last_cycle_gap_seconds", 0)),
            "last_resume_at": int(state.get("last_resume_at", 0)),
            "last_sleep_gap_seconds": int(state.get("last_sleep_gap_seconds", 0)),
            "state_stale": state_stale,
            "controller_connected": versioned["service"]["controller_connected"],
            "state_code": service_state["code"],
            "state_severity": service_state["severity"],
            "state_title": service_state["title"],
            "state_detail": service_state["detail"],
        },
        "policy": {
            "min_samples": MIN_SAMPLES_FOR_OPTIMIZATION,
            "confirmations": PERFORMANCE_CONFIRMATIONS,
            "min_availability": MIN_AVAILABILITY,
            "absolute_gain_ms": MIN_ABSOLUTE_GAIN_MS,
            "relative_gain": MIN_RELATIVE_GAIN,
            "cooldown_seconds": PERFORMANCE_COOLDOWN_SECONDS,
            "failures_before_switch": FAILURES_BEFORE_SWITCH,
            "short_window_size": SHORT_WINDOW_SIZE,
            "long_window_hours": LONG_WINDOW_HOURS,
            "short_min_availability": SHORT_MIN_AVAILABILITY,
            "quarantine_failures": QUARANTINE_FAILURES,
            "quarantine_window_seconds": QUARANTINE_WINDOW_SECONDS,
            "quarantine_seconds": QUARANTINE_SECONDS,
            "quarantine_recovery_successes": QUARANTINE_RECOVERY_SUCCESSES,
            "business_probe_interval_seconds": BUSINESS_PROBE_INTERVAL_SECONDS,
            "standby_probes_per_group": STANDBY_PROBES_PER_GROUP,
        },
        "groups": legacy_groups,
        "nodes": legacy_nodes,
    }
    return {"snapshot_id": snapshot_id, "v1": versioned, "legacy": legacy}


def dashboard_payload(state=None, proxy_data=None, connections=None):
    state = load_state() if state is None else state
    return build_status_snapshots(state, proxy_data or {}, connections or [])["legacy"]


def update_dashboard_cache(snapshots):
    global DASHBOARD_CACHE
    encoded = {
        "snapshot_id": snapshots["snapshot_id"],
        "v1": json.dumps(snapshots["v1"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        "legacy": json.dumps(snapshots["legacy"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    }
    with DASHBOARD_CACHE_LOCK:
        DASHBOARD_CACHE = encoded


def read_dashboard_cache(kind):
    with DASHBOARD_CACHE_LOCK:
        return None if DASHBOARD_CACHE is None else DASHBOARD_CACHE.get(kind)


def cached_api_response(path):
    route = urlsplit(path).path
    kind = "v1" if route == "/api/v1/status" else "legacy" if route == "/api/status" else None
    if kind is None:
        return 404, "application/json; charset=utf-8", b'{"error":"not_found"}'
    content = read_dashboard_cache(kind)
    if content is None:
        return 503, "application/json; charset=utf-8", b'{"error":"snapshot_unavailable"}'
    return 200, "application/json; charset=utf-8", content


def static_acceptance_response(path):
    route = urlsplit(path).path
    target = None
    content_type = None
    if route in ("/acceptance", "/acceptance/"):
        target = ACCEPTANCE_DASHBOARD_PATH
        content_type = "text/html; charset=utf-8"
    elif route == "/acceptance/fixtures":
        target = ACCEPTANCE_FIXTURE_PATH
        content_type = "application/json; charset=utf-8"
    if target is None:
        return 404, "text/plain; charset=utf-8", b"not found"
    try:
        with open(target, "rb") as handle:
            return 200, content_type, handle.read()
    except OSError:
        return 404, "text/plain; charset=utf-8", b"not found"

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        if route in ("/", "/index.html"):
            try:
                with open(DASHBOARD_PATH, "rb") as handle:
                    content = handle.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if route.startswith("/acceptance"):
            status, content_type, content = static_acceptance_response(route)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if route in ("/api/status", "/api/v1/status"):
            status, content_type, content = cached_api_response(route)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_error(404)

    def log_message(self, _format, *_args):
        return


def start_dashboard():
    if read_dashboard_cache("v1") is None:
        state = load_state()
        state["controller_connected"] = False
        update_dashboard_cache(build_status_snapshots(state, {}, []))
    server = http.server.HTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, name="steadyroute-dashboard", daemon=True)
    thread.start()
    log("%s dashboard: http://%s:%d" % (SERVICE_NAME, DASHBOARD_HOST, DASHBOARD_PORT))
    return server


def probe_url(name, url, timeout_ms):
    path = "/proxies/%s/delay?timeout=%d&url=%s" % (
        quote(name, safe=""),
        timeout_ms,
        quote(url, safe=""),
    )
    try:
        response = api_request("GET", path, timeout=(timeout_ms / 1000.0) + 3)
        delay = int(response.get("delay", 0))
        if delay <= 0 or delay >= timeout_ms:
            return None
        return delay
    except Exception:
        return None


def probe_node(name):
    return name, probe_url(name, TEST_URL, PROBE_TIMEOUT_MS)


def record_health_window(node_state, success, delay, observed_at):
    short = list(node_state.get("short_results", []))
    short.append(1 if success else 0)
    node_state["short_results"] = short[-SHORT_WINDOW_SIZE:]

    hour = int(observed_at // 3600)
    buckets = list(node_state.get("long_buckets", []))
    if not buckets or int(buckets[-1].get("hour", -1)) != hour:
        buckets.append({"hour": hour, "success": 0, "total": 0, "latency_sum": 0})
    bucket = buckets[-1]
    bucket["total"] = int(bucket.get("total", 0)) + 1
    if success:
        bucket["success"] = int(bucket.get("success", 0)) + 1
        bucket["latency_sum"] = int(bucket.get("latency_sum", 0)) + int(delay or 0)
    node_state["long_buckets"] = [
        item for item in buckets if hour - int(item.get("hour", hour)) < LONG_WINDOW_HOURS
    ][-LONG_WINDOW_HOURS:]

    failures = [
        int(timestamp) for timestamp in node_state.get("recent_failures", [])
        if observed_at - int(timestamp) <= QUARANTINE_WINDOW_SECONDS
    ]
    if success:
        if int(node_state.get("quarantine_until", 0)):
            node_state["quarantine_recovery_streak"] = int(node_state.get("quarantine_recovery_streak", 0)) + 1
    else:
        failures.append(int(observed_at))
        node_state["quarantine_recovery_streak"] = 0
        if len(failures) >= QUARANTINE_FAILURES:
            node_state["quarantine_until"] = int(observed_at + QUARANTINE_SECONDS)
    node_state["recent_failures"] = failures


def short_availability(node_state):
    values = node_state.get("short_results", [])
    if not values:
        return float(node_state.get("availability_ewma", 0.0))
    return float(sum(values)) / len(values)


def long_availability(node_state):
    buckets = node_state.get("long_buckets", [])
    total = sum(int(item.get("total", 0)) for item in buckets)
    if not total:
        return float(node_state.get("availability_ewma", 0.0))
    successes = sum(int(item.get("success", 0)) for item in buckets)
    return float(successes) / total


def is_quarantined(node_state, now=None):
    now = time.time() if now is None else now
    until = int(node_state.get("quarantine_until", 0))
    if not until:
        return False
    recovered = int(node_state.get("quarantine_recovery_streak", 0)) >= QUARANTINE_RECOVERY_SUCCESSES
    if now >= until and recovered:
        node_state["quarantine_until"] = 0
        node_state["quarantine_recovery_streak"] = 0
        node_state["recent_failures"] = []
        return False
    return True


def update_node_stats(node_state, delay, observed_at=None):
    observed_at = time.time() if observed_at is None else observed_at
    success = delay is not None
    node_state["samples"] = int(node_state.get("samples", 0)) + 1
    previous_availability = node_state.get("availability_ewma")
    current_value = 1.0 if success else 0.0
    if previous_availability is None:
        availability = current_value
    else:
        availability = AVAILABILITY_ALPHA * current_value + (1 - AVAILABILITY_ALPHA) * float(previous_availability)
    node_state["availability_ewma"] = availability
    node_state["last_success"] = success

    if success:
        previous_latency = node_state.get("latency_ewma")
        previous_jitter = float(node_state.get("jitter_ewma", 0.0))
        if previous_latency is None:
            latency = float(delay)
            jitter = 0.0
        else:
            deviation = abs(float(delay) - float(previous_latency))
            jitter = JITTER_ALPHA * deviation + (1 - JITTER_ALPHA) * previous_jitter
            latency = LATENCY_ALPHA * float(delay) + (1 - LATENCY_ALPHA) * float(previous_latency)
        node_state["latency_ewma"] = latency
        node_state["jitter_ewma"] = jitter
        node_state["success_streak"] = int(node_state.get("success_streak", 0)) + 1
        node_state["failure_streak"] = 0
        node_state["last_delay"] = delay
        node_state["last_success_at"] = int(observed_at)
    else:
        node_state["success_streak"] = 0
        node_state["failure_streak"] = int(node_state.get("failure_streak", 0)) + 1
        node_state["last_delay"] = None

    record_health_window(node_state, success, delay, observed_at)

    latency = node_state.get("latency_ewma")
    if latency is None:
        node_state["score"] = 1000000.0
    else:
        node_state["score"] = (
            float(latency)
            + JITTER_WEIGHT * float(node_state.get("jitter_ewma", 0.0))
            + AVAILABILITY_PENALTY_MS * (1.0 - float(availability))
        )


def record_business_result(node_state, success, observed_at):
    node_state["business_checked_at"] = int(observed_at)
    node_state["business_last_success"] = bool(success)
    if success:
        node_state["business_success_streak"] = int(node_state.get("business_success_streak", 0)) + 1
        node_state["business_failure_streak"] = 0
    else:
        node_state["business_success_streak"] = 0
        node_state["business_failure_streak"] = int(node_state.get("business_failure_streak", 0)) + 1


def update_effective_health(node_state, base_success, business_checked, business_success):
    if business_checked:
        if business_success:
            node_state["effective_failure_streak"] = 0
        else:
            # A business failure has already been retried once in the same cycle.
            node_state["effective_failure_streak"] = int(node_state.get("effective_failure_streak", 0)) + 2
    elif base_success:
        node_state["effective_failure_streak"] = 0
    else:
        node_state["effective_failure_streak"] = int(node_state.get("effective_failure_streak", 0)) + 1


def choose_probe_targets(state, proxy_data):
    targets = set()
    for group_name, candidates in GROUPS.items():
        current = (proxy_data.get(group_name) or {}).get("now")
        if current not in candidates:
            current = candidates[0]
        targets.add(current)
        group_state = state["groups"].setdefault(group_name, {})
        standbys = [name for name in candidates if name != current]
        if int(state["nodes"].get(current, {}).get("effective_failure_streak", 0)):
            targets.update(standbys)
            continue
        if standbys:
            cursor = int(group_state.get("standby_probe_cursor", 0))
            for offset in range(min(STANDBY_PROBES_PER_GROUP, len(standbys))):
                targets.add(standbys[(cursor + offset) % len(standbys)])
            group_state["standby_probe_cursor"] = (cursor + STANDBY_PROBES_PER_GROUP) % len(standbys)
    return sorted(targets)


def business_preflight(group_name, node_name, state, dry_run=False):
    urls = BUSINESS_TEST_URLS.get(group_name, [])
    if dry_run or not urls:
        return True
    results = {
        url: probe_url(node_name, url, BUSINESS_PROBE_TIMEOUT_MS)
        for url in urls
    }
    for url in urls:
        if results.get(url) is None:
            results[url] = probe_url(node_name, url, BUSINESS_PROBE_TIMEOUT_MS)
    success = all(results.get(url) is not None for url in urls)
    record_business_result(state["nodes"].setdefault(node_name, {}), success, time.time())
    return success


def active_connections(group_name, connections=None):
    if connections is None:
        try:
            data = api_request("GET", "/connections") or {}
            connections = data.get("connections", [])
        except Exception:
            return []
    return [
        connection for connection in connections
        if group_name in (connection.get("chains") or [])
    ]


def close_old_connections(group_name, old_node, connections=None):
    closed = 0
    for connection in active_connections(group_name, connections):
        chains = connection.get("chains") or []
        connection_id = connection.get("id")
        if connection_id and old_node in chains:
            try:
                api_request("DELETE", "/connections/%s" % quote(connection_id, safe=""), timeout=3)
                closed += 1
            except Exception as error:
                log("could not close connection %s: %s" % (connection_id, error))
    return closed


def select_node(group_name, node_name, dry_run):
    if dry_run:
        log("DRY RUN select %s -> %s" % (group_name, node_name))
        return
    api_request(
        "PUT",
        "/proxies/%s" % quote(group_name, safe=""),
        {"name": node_name},
        timeout=5,
    )


def eligible_for_optimization(node_state):
    return (
        bool(node_state.get("last_success"))
        and int(node_state.get("samples", 0)) >= MIN_SAMPLES_FOR_OPTIMIZATION
        and int(node_state.get("success_streak", 0)) >= MIN_SUCCESS_STREAK
        and float(node_state.get("availability_ewma", 0.0)) >= MIN_AVAILABILITY
        and short_availability(node_state) >= SHORT_MIN_AVAILABILITY
        and long_availability(node_state) >= MIN_AVAILABILITY
        and not is_quarantined(node_state)
    )


def best_failover(candidates, nodes, current):
    available = [
        name for name in candidates
        if name != current
        and nodes.get(name, {}).get("last_success")
        and not is_quarantined(nodes.get(name, {}))
    ]
    if not available:
        available = [
            name for name in candidates
            if name != current and nodes.get(name, {}).get("last_success")
        ]
    if not available:
        return None
    return min(available, key=lambda name: float(nodes[name].get("score", 1000000.0)))


def evaluate_group(group_name, candidates, proxy_data, connections, state, dry_run):
    group_info = proxy_data.get(group_name) or {}
    current = group_info.get("now")
    if current not in candidates:
        log("%s: current selection is unavailable: %r" % (group_name, current))
        current = candidates[0]

    group_state = state["groups"].setdefault(group_name, {})
    previous_seen = group_state.get("last_seen")
    router_choice = group_state.get("last_router_selection")
    now = time.time()

    if previous_seen and current != previous_seen and current != router_choice:
        group_state["manual_hold_until"] = int(now + MANUAL_HOLD_SECONDS)
        group_state["better_candidate"] = None
        group_state["better_streak"] = 0
        log("%s: manual selection detected; optimization paused for 6 hours" % group_name)

    current_stats = state["nodes"].get(current, {})
    current_failures = int(current_stats.get("effective_failure_streak", current_stats.get("failure_streak", 0)))
    if current_failures >= FAILURES_BEFORE_SWITCH:
        remaining = list(candidates)
        target = None
        while remaining:
            candidate = best_failover(remaining, state["nodes"], current)
            if not candidate:
                break
            if business_preflight(group_name, candidate, state, dry_run):
                target = candidate
                break
            remaining.remove(candidate)
            log("%s: skip %s; business preflight failed" % (group_name, candidate))
        if target:
            log("%s: FAILOVER %s -> %s after %d consecutive failures" % (
                group_name, current, target, current_failures
            ))
            select_node(group_name, target, dry_run)
            closed = 0 if dry_run else close_old_connections(group_name, current, connections)
            log("%s: closed %d stale connections" % (group_name, closed))
            group_state["last_router_selection"] = target
            group_state["last_seen"] = target
            group_state["last_switch_at"] = int(now)
            group_state["last_failover_at"] = int(now)
            group_state["recovery_mode"] = True
            group_state["handover_old_node"] = None
            group_state["handover_new_node"] = target
            group_state["handover_grace_until"] = 0
            group_state["recovery_observe_until"] = int(now + 60)
            group_state["better_candidate"] = None
            group_state["better_streak"] = 0
            return
        log("%s: current node failed but no tested backup is available" % group_name)

    active = active_connections(group_name, connections)

    if now < float(group_state.get("manual_hold_until", 0)):
        group_state["last_seen"] = current
        log("%s: keep %s; manual hold is active" % (group_name, current))
        return

    last_switch = float(group_state.get("last_switch_at", 0))
    if not group_state.get("recovery_mode") and now - last_switch < PERFORMANCE_COOLDOWN_SECONDS:
        group_state["last_seen"] = current
        log("%s: keep %s; performance cooldown is active" % (group_name, current))
        return

    eligible = [name for name in candidates if eligible_for_optimization(state["nodes"].get(name, {}))]
    if current not in eligible or len(eligible) < 2:
        group_state["last_seen"] = current
        log("%s: keep %s; collecting weighted history" % (group_name, current))
        return

    leader = min(eligible, key=lambda name: float(state["nodes"][name]["score"]))
    current_score = float(state["nodes"][current]["score"])
    leader_score = float(state["nodes"][leader]["score"])
    absolute_gain = current_score - leader_score
    relative_gain = absolute_gain / max(current_score, 1.0)
    qualifies = (
        leader != current
        and absolute_gain >= MIN_ABSOLUTE_GAIN_MS
        and relative_gain >= MIN_RELATIVE_GAIN
    )

    if not qualifies:
        group_state["better_candidate"] = None
        group_state["better_streak"] = 0
        group_state["last_seen"] = current
        log("%s: keep %s; weighted score %.0f (best %.0f)" % (
            group_name, current, current_score, leader_score
        ))
        return

    if group_state.get("better_candidate") == leader:
        group_state["better_streak"] = int(group_state.get("better_streak", 0)) + 1
    else:
        group_state["better_candidate"] = leader
        group_state["better_streak"] = 1
    confirmations = int(group_state["better_streak"])
    if confirmations < PERFORMANCE_CONFIRMATIONS:
        group_state["last_seen"] = current
        log("%s: candidate %s is better; confirmation %d/%d" % (
            group_name, leader, confirmations, PERFORMANCE_CONFIRMATIONS
        ))
        return

    log("%s: OPTIMIZE %s -> %s; weighted score %.0f -> %.0f" % (
        group_name, current, leader, current_score, leader_score
    ))
    if not business_preflight(group_name, leader, state, dry_run):
        group_state["better_candidate"] = None
        group_state["better_streak"] = 0
        group_state["last_seen"] = current
        log("%s: keep %s; %s failed business preflight" % (group_name, current, leader))
        return
    group_state["pending_decision_events"] = [{
        "code": "handover_pending",
        "reason_code": "confirmation_complete",
        "occurred_at": int(now),
    }]
    select_node(group_name, leader, dry_run)
    group_state["last_router_selection"] = leader
    group_state["last_seen"] = leader
    group_state["last_switch_at"] = int(now)
    group_state["recovery_mode"] = False
    group_state["handover_old_node"] = current
    group_state["handover_new_node"] = leader
    group_state["handover_grace_until"] = int(now + 300)
    group_state["recovery_observe_until"] = int(now + 360)
    group_state["better_candidate"] = None
    group_state["better_streak"] = 0
    if active:
        log("%s: lossless recovery preserved %d existing connections on %s" % (
            group_name, len(active), current
        ))


def run_cycle(dry_run=False):
    cycle_clock = time.perf_counter()
    cycle_started_at = int(time.time())
    state = load_state()
    try:
        proxy_response = api_request("GET", "/proxies") or {}
    except Exception:
        state["controller_connected"] = False
        snapshots = build_status_snapshots(state, {}, [], now=cycle_started_at)
        save_state(state)
        update_dashboard_cache(snapshots)
        raise
    proxy_data = proxy_response.get("proxies") or {}
    state["controller_connected"] = True
    try:
        connection_response = api_request("GET", "/connections") or {}
        connections = connection_response.get("connections") or []
    except Exception:
        connections = []
    targets = choose_probe_targets(state, proxy_data)
    base_results = {}
    business_results = {}
    future_meta = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for name in targets:
            future = executor.submit(probe_url, name, TEST_URL, PROBE_TIMEOUT_MS)
            future_meta[future] = ("base", None, name, TEST_URL)
        for group_name, candidates in GROUPS.items():
            current = (proxy_data.get(group_name) or {}).get("now")
            if current not in candidates:
                current = candidates[0]
            group_state = state["groups"].setdefault(group_name, {})
            current_state = state["nodes"].get(current, {})
            business_due = (
                cycle_started_at - int(group_state.get("last_business_probe_at", 0)) >= BUSINESS_PROBE_INTERVAL_SECONDS
                or int(current_state.get("effective_failure_streak", 0)) > 0
            )
            urls = BUSINESS_TEST_URLS.get(group_name, [])
            if business_due and urls:
                cursor = int(group_state.get("business_probe_cursor", 0))
                url = urls[cursor % len(urls)]
                group_state["business_probe_cursor"] = (cursor + 1) % len(urls)
                group_state["last_business_probe_at"] = cycle_started_at
                future = executor.submit(probe_url, current, url, BUSINESS_PROBE_TIMEOUT_MS)
                future_meta[future] = ("business", group_name, current, url)
        for future in concurrent.futures.as_completed(future_meta):
            kind, group_name, name, url = future_meta[future]
            delay = future.result()
            if kind == "base":
                base_results[name] = delay
            else:
                business_results[group_name] = {"name": name, "url": url, "delay": delay}

    for name in targets:
        node_state = state["nodes"].setdefault(name, {})
        update_node_stats(node_state, base_results.get(name), cycle_started_at)
        node_state["last_probe_at"] = cycle_started_at

    for group_name, candidates in GROUPS.items():
        current = (proxy_data.get(group_name) or {}).get("now")
        if current not in candidates:
            current = candidates[0]
        node_state = state["nodes"].setdefault(current, {})
        business = business_results.get(group_name)
        business_checked = business is not None
        business_success = False
        if business_checked:
            business_success = business["delay"] is not None
            if not business_success:
                retry_delay = probe_url(current, business["url"], BUSINESS_PROBE_TIMEOUT_MS)
                business_success = retry_delay is not None
                business["delay"] = retry_delay
            record_business_result(node_state, business_success, cycle_started_at)
        update_effective_health(
            node_state,
            base_results.get(current) is not None,
            business_checked,
            business_success,
        )

    summary = []
    for name in targets:
        stats = state["nodes"][name]
        summary.append("%s=%s/score%.0f" % (
            name,
            ("%dms" % base_results[name]) if base_results.get(name) is not None else "FAIL",
            float(stats.get("score", 1000000.0)),
        ))
    business_summary = [
        "%s:%s=%s" % (
            group_name,
            result["url"].split("/")[2],
            ("%dms" % result["delay"]) if result.get("delay") is not None else "FAIL",
        )
        for group_name, result in business_results.items()
    ]
    log("probe: " + " | ".join(summary) + (" | business " + " | ".join(business_summary) if business_summary else ""))

    for group_name, candidates in GROUPS.items():
        evaluate_group(group_name, candidates, proxy_data, connections, state, dry_run)
    previous_update = int(state.get("updated_at", 0))
    if previous_update:
        gap = max(0, cycle_started_at - previous_update)
        state["last_cycle_gap_seconds"] = gap
        if gap > PROBE_INTERVAL_SECONDS * 3:
            state["last_resume_at"] = cycle_started_at
            state["last_sleep_gap_seconds"] = gap
            log("resume detected after %d seconds without probes" % gap)
    state["updated_at"] = cycle_started_at
    state["last_cycle_duration_ms"] = int(round((time.perf_counter() - cycle_clock) * 1000))
    snapshots = build_status_snapshots(state, proxy_data, connections, now=cycle_started_at)
    save_state(state)
    update_dashboard_cache(snapshots)


def show_status():
    state = load_state()
    output = {"groups": state.get("groups", {}), "nodes": {}}
    for name, stats in state.get("nodes", {}).items():
        output["nodes"][name] = {
            "availability": round(float(stats.get("availability_ewma", 0.0)), 3),
            "latency": round(float(stats.get("latency_ewma", 0.0)), 1) if stats.get("latency_ewma") is not None else None,
            "jitter": round(float(stats.get("jitter_ewma", 0.0)), 1),
            "score": round(float(stats.get("score", 1000000.0)), 1),
            "success_streak": stats.get("success_streak", 0),
            "failure_streak": stats.get("failure_streak", 0),
            "samples": stats.get("samples", 0),
        }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    os.makedirs(BASE_DIR, exist_ok=True)
    if args.status:
        show_status()
        return 0

    lock_handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another router instance is already running")
        return 0

    dashboard_server = None
    if args.daemon:
        try:
            dashboard_server = start_dashboard()
        except Exception as error:
            log("dashboard failed to start: %s" % error)

    while True:
        try:
            run_cycle(dry_run=args.dry_run)
        except Exception as error:
            log("cycle failed: %s" % error)
        if not args.daemon:
            break
        time.sleep(PROBE_INTERVAL_SECONDS)
    if dashboard_server is not None:
        dashboard_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
