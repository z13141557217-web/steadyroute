#!/usr/bin/python3
"""Weighted, connection-aware selector for Clash Verge residential routes."""

import argparse
import datetime
import collections
import concurrent.futures
import fcntl
import http.server
import json
import logging
import os
import resource
import socket
import stat
import tempfile
import sys
import threading
import time
from urllib.parse import quote, urlsplit

try:
    import state_contract
    import candidate_registry
    import route_policy
    import health_model
    import logging_setup
    import runtime_metrics
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import state_contract
    import candidate_registry
    import route_policy
    import health_model
    import logging_setup
    import runtime_metrics


CONTROLLER_SOCKET_PATHS = (
    "/var/run/clash-verge-service/users/%d/verge-mihomo.sock" % os.getuid(),
    os.path.join(tempfile.gettempdir(), "verge-mihomo.sock"),
    "/tmp/verge/verge-mihomo.sock",
)
BASE_DIR = "/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router"
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOCK_PATH = os.path.join(BASE_DIR, "router.lock")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ACCEPTANCE_DASHBOARD_PATH = os.path.join(APP_DIR, "acceptance_dashboard.html")
ACCEPTANCE_FIXTURE_PATH = os.path.join(APP_DIR, "fixtures", "status_contract_v2.json")
CANDIDATE_DASHBOARD_PATH = os.path.join(APP_DIR, "candidate_dashboard.html")
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 17654
SERVICE_NAME = "稳航 SteadyRoute"
SERVICE_STARTED_AT = int(time.time())
DASHBOARD_CACHE = None
DASHBOARD_CACHE_LOCK = threading.Lock()
TEST_URL = "https://cp.cloudflare.com/generate_204"
PROBE_TIMEOUT_MS = 3000
BUSINESS_PROBE_TIMEOUT_MS = 5000
PROBE_WORKERS = 10
PROBE_INTERVAL_SECONDS = 20
CYCLE_BUDGET_SECONDS = 10
CYCLE_DURATION_SAMPLES = 60
SWITCH_HISTORY_LIMIT = 10
PREFLIGHT_FRESH_SECONDS = 120
BUSINESS_TARGET_DOWN_SECONDS = 300
CONFIRM_TIMEOUT_MS = 2000
CONFIRM_PROBES = 2
CONFIRM_STAGGER_SECONDS = 0.7
FAST_PROBE_INTERVAL_SECONDS = 5
FAST_PROBE_URL = "http://cp.cloudflare.com/generate_204"
FAST_PROBE_TIMEOUT_MS = 2000
TIMELINE_WINDOW_SECONDS = 30 * 60     # legacy API: dashboard chart offers 5 / 15 / 30 minutes
V1_TIMELINE_WINDOW_SECONDS = 300      # v1 recent_probes keeps its original 5-minute contract
TIMELINE_LIMIT = 480                  # 360 fast-lane points + 90 standby points + switches
CONNECTION_SITE_LIMIT = 30
LOG_DIR = os.environ.get("STEADYROUTE_LOG_DIR", "/Users/nurture/Library/Logs/Clash-Verge-Stability-Router")
MEMORY_SAMPLE_SECONDS = 600
MEMORY_SAMPLE_LIMIT = 144
EVENT_KEY_LIMIT = 1000
LOCAL_CHECK_TIMEOUT_MS = 3000
LOCAL_CHECK_URLS = (
    "http://captive.apple.com/hotspot-detect.html",
    "https://www.baidu.com/favicon.ico",
)
FAILOVER_STORM_WINDOW_SECONDS = 10 * 60
FAILOVER_STORM_THRESHOLD = 3
MASS_FAILURE_MIN_TARGETS = 3
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
MANUAL_HOLD_SECONDS = 60 * 60
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

# In-memory only: never persisted, reset on process start.
RUNTIME = {"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0}
TIMELINE = {}   # group -> recent current-node probes and switches (memory only)
WAKE_EVENT = threading.Event()

POLICY_CONFIG_PATHS = (
    os.path.join(APP_DIR, "config", "route-policies.json"),
    os.path.join(os.path.dirname(os.path.dirname(APP_DIR)), "config", "route-policies.json"),
)


def load_runtime_policy_config():
    for path in POLICY_CONFIG_PATHS:
        if os.path.exists(path):
            return route_policy.load_policy_config(path)
    raise route_policy.PolicyConfigError("route-policies.json is unavailable")


POLICY_CONFIG = load_runtime_policy_config()
POLICIES = list(POLICY_CONFIG["policies"])
POLICY_BY_GROUP = {item["group_name"]: item for item in POLICIES}
GROUPS = {item["group_name"]: list(item["static_candidates"]) for item in POLICIES}
BUSINESS_TEST_URLS = {item["group_name"]: list(item["business_test_urls"]) for item in POLICIES}


LOGGER = logging.getLogger("steadyroute")
ROUTINE = logging_setup.RoutineLimiter()


def log(message):
    LOGGER.info(message)


def log_warning(message, exc_info=False):
    LOGGER.warning(message, exc_info=exc_info)


def log_error(message, exc_info=False):
    LOGGER.error(message, exc_info=exc_info)


def log_routine(key, meaning, message, now=None):
    """Routine lines are written when their meaning changes or every 10 minutes."""
    emit, suppressed = ROUTINE.should_emit(key, meaning, time.time() if now is None else now)
    if emit:
        LOGGER.info(message + ("（此前同类说明省略 %d 次）" % suppressed if suppressed else ""))


def recent_points(group_name, count=6):
    return [[item["t"], item.get("ms")] for item in TIMELINE.get(group_name, []) if item.get("kind") == "probe"][-count:]


def record_manual_preference_event(state, group_name, code, reason, now):
    event = {
        "code": code,
        "severity": "info" if code != "MANUAL_PREFERENCE_INTERRUPTED" else "warning",
        "scope": "group",
        "subject_id": state_contract.group_ui_id(group_name),
        "group_id": state_contract.group_ui_id(group_name),
        "node_id": None,
        "from_state": "manual_hold" if code != "MANUAL_PREFERENCE_STARTED" else None,
        "to_state": "manual_hold" if code == "MANUAL_PREFERENCE_STARTED" else None,
        "reason_code": reason,
        "occurred_at": int(now),
        "occurred_at_iso": state_contract.utc_iso(now),
    }
    events = list(state.get("events", []))
    events.append(event)
    state["events"] = events[-state_contract.EVENT_LIMIT:]


def record_runtime_event(state, code, reason, now, group_name=None, severity="warning",
                         from_state=None, to_state=None):
    """Append a whitelisted operational event. Never include URLs or node addresses."""
    group_id = state_contract.group_ui_id(group_name) if group_name else None
    event = {
        "code": code,
        "severity": severity,
        "scope": "group" if group_name else "service",
        "subject_id": group_id or "service",
        "group_id": group_id,
        "node_id": None,
        "from_state": from_state,
        "to_state": to_state,
        "reason_code": reason,
        "occurred_at": int(now),
        "occurred_at_iso": state_contract.utc_iso(now),
    }
    events = list(state.get("events", []))
    events.append(event)
    state["events"] = events[-state_contract.EVENT_LIMIT:]


def _event_key(event):
    return json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sync_state_events(state, write=True):
    """Mirror new dashboard events (state["events"]) into events.jsonl exactly once.

    The first call only seeds the seen-set, so a restart never re-writes old events.
    """
    seen = RUNTIME.get("event_keys")
    first = seen is None
    if first:
        seen = RUNTIME["event_keys"] = collections.OrderedDict()
    for event in state.get("events", []):
        key = _event_key(event)
        if key in seen:
            continue
        seen[key] = True
        if write and not first:
            fields = {name: value for name, value in event.items() if name != "occurred_at_iso"}
            # Node lifecycle flips are frequent on flaky nodes; keep them out of the
            # 90-day decision log so they can never push failover records out.
            if event.get("scope") == "node":
                logging_setup.write_node_event("state_event", **fields)
            else:
                logging_setup.write_event("state_event", **fields)
    while len(seen) > EVENT_KEY_LIMIT:
        seen.popitem(last=False)


def resolve_controller_socket():
    configured = os.environ.get("STEADYROUTE_CONTROLLER_SOCKET")
    candidates = ([configured] if configured else []) + list(CONTROLLER_SOCKET_PATHS)
    seen = set()
    for path in candidates:
        if not path or path in seen or not os.path.isabs(path):
            continue
        seen.add(path)
        try:
            metadata = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISSOCK(metadata.st_mode):
            return path
    raise RuntimeError("Mihomo controller socket is unavailable")


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
        client.connect(resolve_controller_socket())
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
    """Peak resident memory of this process (kept as memory_mb for compatibility)."""
    return runtime_metrics.peak_rss_mb()


def footprint_now():
    """Current footprint plus a same-scale peak (OS peak, else the highest value seen)."""
    current = runtime_metrics.current_footprint_mb()
    RUNTIME["memory_current_mb"] = current
    peaks = [value for value in (runtime_metrics.peak_footprint_mb(), RUNTIME.get("memory_footprint_peak_mb"), current)
             if value is not None]
    RUNTIME["memory_footprint_peak_mb"] = max(peaks) if peaks else None
    return current


def record_memory_sample(state, now):
    """Keep one current-footprint sample every MEMORY_SAMPLE_SECONDS for this process only."""
    current = footprint_now()
    if current is None:
        return
    if state.get("memory_samples_started_at") != SERVICE_STARTED_AT:
        state["memory_samples_started_at"] = SERVICE_STARTED_AT
        state["memory_samples"] = []
    samples = state.get("memory_samples") or []
    if samples and now - int(samples[-1][0]) < MEMORY_SAMPLE_SECONDS:
        return
    state["memory_samples"] = health_model.bounded_append(samples, [int(now), current], MEMORY_SAMPLE_LIMIT)


def merge_legacy_cycle_count(state):
    """One-time: continue the count the pre-0.4.3 dashboard showed (max node samples)."""
    if state.get("cycle_count_merged"):
        return
    legacy = max([int(item.get("samples", 0)) for item in state.get("nodes", {}).values()] or [0])
    state["cycle_count"] = max(int(state.get("cycle_count", 0)), legacy)
    state["cycle_count_merged"] = True


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
    policy = POLICY_BY_GROUP.get(group_name)
    return policy["region"] if policy else "unknown"


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
    safe_backup_available = any(
        name != current and eligible_for_optimization(state.get("nodes", {}).get(name, {}))
        for name in candidates
    )
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
        "performance_optimization_paused": manual_hold > 0,
        "safety_failover_active": bool(state.get("controller_connected", bool(proxy_data)) and candidates),
        "safe_backup_available": safe_backup_available,
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
    try:
        for code in state_contract.transition_path(scope, cursor, new_state):
            if not steps or steps[-1][0] != code:
                steps.append((code, transition_reason(scope, code, final_decision=final_decision)))
        return state_contract.record_transition_path(
            state, scope, subject_id, old_state, new_state, steps, occurred_at,
            group_id=group_id,
        )
    except state_contract.InvalidTransitionError as error:
        # Event bookkeeping must never abort a cycle after a node was already selected:
        # that would drop the cycle's state (switch time, cooldown, handover) on the floor.
        log_warning("unmodelled %s transition %s -> %s recorded directly (%s)" % (
            scope, old_state, new_state, error))
        return state_contract.record_unmodelled_transition(
            state, scope, subject_id, old_state, new_state, occurred_at, group_id=group_id)


def public_timeline(group_name, now, window=None):
    """Compact live-strip points: [unix_seconds, ms_or_null, kind, node, reason].

    kind: "probe" (current node, every 5 s), "standby" (hot standby, every 20 s), "switch".
    reason (switch only): "failover" | "optimize" | "removed"; null otherwise.
    """
    cutoff = float(now) - (TIMELINE_WINDOW_SECONDS if window is None else window) - 30
    return [
        [item["t"], item.get("ms"), item.get("kind", "probe"), item.get("node"), item.get("reason")]
        for item in TIMELINE.get(group_name, []) if float(item["t"]) >= cutoff
    ]


def _connection_started_at(value):
    """Mihomo reports RFC 3339 with nanoseconds ("2026-09-26T08:01:02.123456789+08:00")."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        base = datetime.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    rest = value[19:]
    while rest[:1] == "." or rest[:1].isdigit():
        rest = rest[1:]
    if rest in ("", "Z", "z"):
        offset = 0
    else:
        try:
            sign = -1 if rest[0] == "-" else 1
            hours, minutes = rest[1:].split(":")
            offset = sign * (int(hours) * 3600 + int(minutes) * 60)
        except (ValueError, IndexError):
            return None
    return int((base - datetime.datetime(1970, 1, 1)).total_seconds()) - offset


def connection_summary(group_name, connections, now):
    """Per-site view of the connections routed through a group, for the dashboard only.

    Shows host names (never IP addresses or full URLs); nothing is persisted or logged.
    """
    sites = {}
    by_node = {}
    total = 0
    for connection in connections or []:
        chains = connection.get("chains") or []
        if group_name not in chains:
            continue
        total += 1
        node = chains[0] if chains else None
        by_node[node] = by_node.get(node, 0) + 1
        metadata = connection.get("metadata") or {}
        host = metadata.get("host") or metadata.get("sniffHost") or ""
        host = host if host and not host.replace(".", "").replace(":", "").isdigit() else "IP 直连（地址已隐藏）"
        process = os.path.basename(str(metadata.get("process") or metadata.get("processPath") or "")) or None
        site = sites.setdefault(host, {
            "host": host, "count": 0, "nodes": {}, "upload": 0, "download": 0,
            "since": None, "process": process,
        })
        site["count"] += 1
        site["nodes"][node] = site["nodes"].get(node, 0) + 1
        site["upload"] += int(connection.get("upload") or 0)
        site["download"] += int(connection.get("download") or 0)
        started = _connection_started_at(connection.get("start"))
        if started is not None and started <= now:
            site["since"] = started if site["since"] is None else min(site["since"], started)
        if not site["process"] and process:
            site["process"] = process
    ordered = sorted(sites.values(), key=lambda item: (-item["count"], -(item["upload"] + item["download"])))
    return {
        "total": total,
        "by_node": by_node,
        "sites": ordered[:CONNECTION_SITE_LIMIT],
        "more_sites": max(0, len(ordered) - CONNECTION_SITE_LIMIT),
        "observed_at": int(now),
    }


def build_status_snapshots(state, proxy_data, connections, now=None, memory_mb=None):
    now = int(time.time()) if now is None else int(now)
    memory_mb = memory_megabytes() if memory_mb is None else float(memory_mb)
    migrated = state_contract.migrate_state(state)
    state.clear()
    state.update(migrated)
    last_cycle = state.get("updated_at")
    stale_after = PROBE_INTERVAL_SECONDS * 2 + CYCLE_BUDGET_SECONDS
    state_stale = last_cycle is None or now - int(last_cycle) > stale_after
    cycle_started = state.get("last_cycle_started_at", last_cycle)
    stale_at = int(last_cycle) + stale_after if last_cycle is not None else None
    stale_title = "检测延迟"
    stale_detail = "超过 %d 秒没有完成检测周期，页面数据可能已过时。" % stale_after
    durations = [int(value) for value in state.get("cycle_durations_ms", [])]
    resumed = bool(state.get("last_resume_at") and now - int(state.get("last_resume_at")) < 180)
    if state_stale:
        service_state = {
            "code": "stale", "severity": "warning", "title": "检测暂停",
            "detail": "最后成功周期已超过新鲜度门槛。", "next_action": "等待下一次成功检测周期。",
        }
    elif state.get("local_network_ok") is False:
        service_state = {
            "code": "local_network_offline", "severity": "warning", "title": "本机网络不可用",
            "detail": "直连检测失败，已暂停故障判定和切换，避免误伤节点。",
            "next_action": "网络恢复后自动继续检测。",
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
            "hot_standby": (
                state_contract.node_ui_id(group_name, group_state["hot_standby"])
                if group_state.get("hot_standby") else None
            ),
            "recent_probes": public_timeline(group_name, now, V1_TIMELINE_WINDOW_SECONDS),
            "business_targets_down": sum(
                1 for until in (group_state.get("business_target_down") or {}).values()
                if int(until) > now
            ),
            "metrics": {
                "failovers_24h": health_model.count_recent(group_state.get("failover_times"), now, 86400),
                "performance_switches_24h": health_model.count_recent(
                    group_state.get("performance_switch_times"), now, 86400),
                "last_failover_detect_seconds": group_state.get("last_failover_detect_seconds"),
            },
            "automation": {
                "performance_optimization_paused": facts["performance_optimization_paused"],
                "safety_failover_active": facts["safety_failover_active"],
                "safe_backup_available": facts["safe_backup_available"],
                "manual_preference_remaining_seconds": facts["manual_hold_remaining_seconds"],
                "manual_preference_remaining_text": "%02d:%02d" % divmod(
                    facts["manual_hold_remaining_seconds"], 60
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
            "last_cycle_started_at": int(cycle_started) if cycle_started is not None else None,
            "cycle_count": int(state.get("cycle_count", 0)),
            "last_probe_at": RUNTIME.get("last_probe_at") or (int(last_cycle) if last_cycle is not None else None),
            "fast_probe_interval_seconds": FAST_PROBE_INTERVAL_SECONDS,
            "cycle_duration_p50_ms": health_model.optional_percentile(durations, 0.5),
            "cycle_duration_p95_ms": health_model.optional_percentile(durations, 0.95),
            "next_cycle_at": int(cycle_started) + PROBE_INTERVAL_SECONDS if cycle_started is not None else None,
            "next_cycle_at_iso": iso_timestamp(int(cycle_started) + PROBE_INTERVAL_SECONDS) if cycle_started is not None else None,
            "stale_at": stale_at,
            "stale_at_iso": iso_timestamp(stale_at),
            "stale_title": stale_title,
            "stale_detail": stale_detail,
            "local_network_ok": state.get("local_network_ok"),
            "state_stale": state_stale,
            "stale_after_seconds": stale_after,
            "memory_mb": memory_mb,
            "memory_peak_mb": RUNTIME.get("memory_footprint_peak_mb") or memory_mb,
            "memory_current_mb": RUNTIME.get("memory_current_mb"),
            "memory_trend_mb_per_hour": (
                runtime_metrics.trend_mb_per_hour(state.get("memory_samples"))
                if state.get("memory_samples_started_at") == SERVICE_STARTED_AT else None
            ),
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
            "dynamic": candidate_registry.public_snapshot(POLICY_CONFIG, state, proxy_data, now),
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
            "hot_standby": group_state.get("hot_standby"),
            "metrics": group["metrics"],
            "connections": connection_summary(group["name"], connections, now),
            "timeline": public_timeline(group["name"], now),
        })
    legacy = {
        "snapshot_id": snapshot_id,
        "service": {
            "name": SERVICE_NAME,
            "status": "running",
            "started_at": SERVICE_STARTED_AT,
            "updated_at": int(last_cycle) if last_cycle is not None else None,
            "memory_mb": memory_mb,
            "memory_peak_mb": RUNTIME.get("memory_footprint_peak_mb") or memory_mb,
            "memory_current_mb": RUNTIME.get("memory_current_mb"),
            "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
            "last_cycle_gap_seconds": int(state.get("last_cycle_gap_seconds", 0)),
            "last_resume_at": int(state.get("last_resume_at", 0)),
            "last_sleep_gap_seconds": int(state.get("last_sleep_gap_seconds", 0)),
            "cycle_count": int(state.get("cycle_count", 0)),
            "last_probe_at": RUNTIME.get("last_probe_at") or (int(last_cycle) if last_cycle is not None else None),
            "fast_probe_interval_seconds": FAST_PROBE_INTERVAL_SECONDS,
            "timeline_window_seconds": TIMELINE_WINDOW_SECONDS,
            "version": versioned["service"]["version"],
            "stale_at": stale_at,
            "stale_title": stale_title,
            "stale_detail": stale_detail,
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
            "confirm_probes": 1 + CONFIRM_PROBES,
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
    elif route in ("/candidate-acceptance", "/candidate-acceptance/"):
        target = CANDIDATE_DASHBOARD_PATH
        content_type = "text/html; charset=utf-8"
    if target is None:
        return 404, "text/plain; charset=utf-8", b"not found"
    try:
        with open(target, "rb") as handle:
            return 200, content_type, handle.read()
    except OSError:
        return 404, "text/plain; charset=utf-8", b"not found"

EXPECTED_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)


class QuietHTTPServer(http.server.ThreadingHTTPServer):
    """A browser closing a tab mid-response is normal; don't print a traceback for it."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, EXPECTED_DISCONNECTS):
            return
        log_warning("dashboard request failed: %s" % error, exc_info=True)


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    timeout = 15

    def _send(self, content):
        try:
            self.wfile.write(content)
        except EXPECTED_DISCONNECTS:
            self.close_connection = True

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
            self._send(content)
            return
        if route.startswith("/acceptance") or route.startswith("/candidate-acceptance"):
            status, content_type, content = static_acceptance_response(route)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self._send(content)
            return
        if route in ("/api/status", "/api/v1/status"):
            status, content_type, content = cached_api_response(route)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self._send(content)
            return
        self.send_error(404)

    def log_message(self, _format, *_args):
        return


def start_dashboard():
    if read_dashboard_cache("v1") is None:
        state = load_state()
        state["controller_connected"] = False
        update_dashboard_cache(build_status_snapshots(state, {}, []))
    server = QuietHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
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


PROBE_POOL = None
PROBE_POOL_LOCK = threading.Lock()


def probe_pool():
    """One long-lived pool: the fast lane runs every 5 s and must not spawn threads each time."""
    global PROBE_POOL
    with PROBE_POOL_LOCK:
        if PROBE_POOL is None:
            PROBE_POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=PROBE_WORKERS, thread_name_prefix="steadyroute-probe")
        return PROBE_POOL


def run_probe_jobs(jobs):
    """jobs: list of (key, node_name, url, timeout_ms). Returns {key: delay_or_None}.

    All jobs run concurrently (up to PROBE_WORKERS), so a batch takes as long as its slowest probe.
    """
    if not jobs:
        return {}
    results = {}
    executor = probe_pool()
    futures = {
        executor.submit(probe_url, name, url, timeout_ms): key
        for key, name, url, timeout_ms in jobs
    }
    for future in concurrent.futures.as_completed(futures):
        results[futures[future]] = future.result()
    return results


def probe_many(jobs, timeout_ms):
    """jobs: list of (key, node_name, url). Runs in parallel with one timeout."""
    return run_probe_jobs([(key, name, url, timeout_ms) for key, name, url in jobs])


def local_network_ok():
    """Probe DIRECT through Mihomo's delay API. Read-only: never selects DIRECT."""
    results = probe_many([(url, "DIRECT", url) for url in LOCAL_CHECK_URLS], LOCAL_CHECK_TIMEOUT_MS)
    return any(value is not None for value in results.values())


def ensure_local_check(cycle_cache):
    if "local_ok" not in cycle_cache:
        cycle_cache["local_ok"] = local_network_ok()
    return cycle_cache["local_ok"]


def confirm_current_failure(current, cycle_cache):
    """Return 'confirmed' | 'transient' | 'local_offline'.

    Runs after one failed probe of the current node. The confirmation probes run
    in parallel, a little staggered, together with the read-only local-network
    check, so a dead node is confirmed in about CONFIRM_TIMEOUT_MS plus the stagger.
    cycle_cache is shared within one cycle so the local check runs at most once.
    """
    need_local = "local_ok" not in cycle_cache
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIRM_PROBES + 1) as executor:
        local = executor.submit(local_network_ok) if need_local else None
        confirms = []
        for index in range(CONFIRM_PROBES):
            if index:
                time.sleep(CONFIRM_STAGGER_SECONDS)
            confirms.append(executor.submit(probe_url, current, FAST_PROBE_URL, CONFIRM_TIMEOUT_MS))
        delays = [future.result() for future in confirms]
        if local is not None:
            cycle_cache["local_ok"] = local.result()
    if not cycle_cache["local_ok"]:
        return "local_offline"
    if any(delay is not None for delay in delays):
        return "transient"
    return "confirmed"


def timeline_add(group_name, point):
    """Keep a short in-memory history for the dashboard's live strip."""
    items = TIMELINE.setdefault(group_name, [])
    items.append(point)
    cutoff = float(point["t"]) - TIMELINE_WINDOW_SECONDS - 30
    TIMELINE[group_name] = [item for item in items if float(item["t"]) >= cutoff][-TIMELINE_LIMIT:]


def record_health_window(node_state, success, delay, observed_at, quarantine=True):
    """quarantine=False: a failure the same cycle proved transient; it lowers availability
    but never counts toward the 3-failures-in-10-minutes quarantine."""
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
    elif quarantine:
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


def update_node_stats(node_state, delay, observed_at=None, quarantine=True):
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
        node_state.pop("first_failure_at", None)
    else:
        if not int(node_state.get("failure_streak", 0)):
            node_state["first_failure_at"] = int(observed_at)
        node_state["success_streak"] = 0
        node_state["failure_streak"] = int(node_state.get("failure_streak", 0)) + 1
        node_state["last_delay"] = None

    record_health_window(node_state, success, delay, observed_at, quarantine=quarantine)

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
        node_state["business_successes"] = int(node_state.get("business_successes", 0)) + 1
        node_state["business_success_streak"] = int(node_state.get("business_success_streak", 0)) + 1
        node_state["business_failure_streak"] = 0
    else:
        node_state["business_success_streak"] = 0
        node_state["business_failure_streak"] = int(node_state.get("business_failure_streak", 0)) + 1


def update_effective_health(node_state, base_success, business_checked, business_success,
                            business_verdict=None, record_failures=True):
    """Update the failure streak that drives failover.

    business_verdict (when given) overrides business_checked/business_success:
      "ok"      business URL reachable through this node
      "node"    this node failed while the hot standby reached the same URL
      "target"  the hot standby failed too: the site is down, not the node
      "unknown" failed with no standby to compare against
    With record_failures False (local outage or wake-up cycle) failures never
    increase the streak; successes still reset it.
    """
    previous = int(node_state.get("effective_failure_streak", 0))
    if business_verdict is None and business_checked:
        business_verdict = "ok" if business_success else "node"
    if business_verdict == "ok":
        node_state["effective_failure_streak"] = 0
    elif business_verdict == "node":
        # The business failure has already been retried once in the same cycle.
        node_state["effective_failure_streak"] = previous + 2 if record_failures else previous
    elif business_verdict == "unknown":
        node_state["effective_failure_streak"] = previous + 1 if record_failures else previous
    elif base_success:
        node_state["effective_failure_streak"] = 0
    else:
        node_state["effective_failure_streak"] = previous + 1 if record_failures else previous


def select_hot_standby(candidates, nodes, current):
    """The standby a failover would pick right now; probed every cycle to stay fresh."""
    return best_failover(candidates, nodes, current)


def choose_probe_targets(state, proxy_data):
    targets = set()
    for group_name, candidates in GROUPS.items():
        current = (proxy_data.get(group_name) or {}).get("now")
        if current not in candidates:
            current = candidates[0]
        targets.add(current)
        group_state = state["groups"].setdefault(group_name, {})
        hot = select_hot_standby(candidates, state["nodes"], current)
        group_state["hot_standby"] = hot
        if hot:
            targets.add(hot)
        standbys = [name for name in candidates if name != current]
        if int(state["nodes"].get(current, {}).get("effective_failure_streak", 0)):
            targets.update(standbys)
            continue
        now = time.time()
        rotation = [
            name for name in standbys
            if name != hot and int(state["nodes"].get(name, {}).get("quarantine_until", 0)) <= now
        ]
        if rotation:
            cursor = int(group_state.get("standby_probe_cursor", 0))
            for offset in range(min(STANDBY_PROBES_PER_GROUP, len(rotation))):
                targets.add(rotation[(cursor + offset) % len(rotation)])
            group_state["standby_probe_cursor"] = (cursor + STANDBY_PROBES_PER_GROUP) % len(rotation)
    targets.update(name for _policy, name in candidate_registry.warmup_probe_targets(POLICY_CONFIG, state))
    return sorted(targets)


def target_down_urls(state, group_name, now):
    """Business URLs currently judged down on the site side for this group."""
    marks = state.get("groups", {}).get(group_name, {}).get("business_target_down") or {}
    return {url for url, until in marks.items() if int(until) > int(now)}


def preflight_is_fresh(node_state, now):
    return bool(
        node_state.get("business_last_success")
        and now - int(node_state.get("business_checked_at", 0)) <= PREFLIGHT_FRESH_SECONDS
        and node_state.get("last_success")
        and now - int(node_state.get("last_probe_at", 0)) <= PROBE_INTERVAL_SECONDS + CYCLE_BUDGET_SECONDS
    )


def business_preflight(group_name, node_name, state, dry_run=False, now=None, allow_fresh_skip=True):
    now = time.time() if now is None else now
    down = target_down_urls(state, group_name, now)
    urls = [url for url in BUSINESS_TEST_URLS.get(group_name, []) if url not in down]
    if dry_run or not urls:
        return True
    node_state = state["nodes"].setdefault(node_name, {})
    if allow_fresh_skip and preflight_is_fresh(node_state, now):
        node_state["preflight_skipped_at"] = int(now)
        return True
    first = probe_many([(url, node_name, url) for url in urls], BUSINESS_PROBE_TIMEOUT_MS)
    failed = [url for url in urls if first.get(url) is None]
    retry = probe_many([(url, node_name, url) for url in failed], BUSINESS_PROBE_TIMEOUT_MS)
    success = all(first.get(url) is not None or retry.get(url) is not None for url in urls)
    record_business_result(node_state, success, now)
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
                log_warning("could not close connection %s: %s" % (connection_id, error))
    return closed


class RegionGuardError(RuntimeError):
    """A selection would leave the group's own region/residential candidate set."""


def selection_allowed(group_name, node_name):
    """Iron rule: a group may only ever select its own region's residential nodes."""
    policy = POLICY_BY_GROUP.get(group_name)
    if policy is None or not isinstance(node_name, str):
        return False
    if node_name in route_policy.BUILTIN_CANDIDATES:
        return False
    if not route_policy.name_matches(policy, node_name):
        return False
    return all(
        not route_policy.name_matches(other, node_name)
        for other in POLICIES if other["group_name"] != group_name
    )


def select_node(group_name, node_name, dry_run):
    if not selection_allowed(group_name, node_name):
        log_warning("%s: BLOCKED selection of %r; outside this region's residential nodes" % (group_name, node_name))
        logging_setup.write_event("region_guard_blocked", group=group_name, node=node_name)
        raise RegionGuardError("refusing cross-region or non-residential selection: %s -> %s" % (group_name, node_name))
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
        and eligible_for_optimization(nodes.get(name, {}))
    ]
    if not available:
        return None
    return min(available, key=lambda name: float(nodes[name].get("score", 1000000.0)))


def evaluate_group(group_name, candidates, proxy_data, connections, state, dry_run):
    group_info = proxy_data.get(group_name) or {}
    current = group_info.get("now")
    group_state = state["groups"].setdefault(group_name, {})
    if group_name in POLICY_BY_GROUP:
        # Iron rule: only this region's residential nodes are ever considered.
        candidates = [name for name in candidates if selection_allowed(group_name, name)]
    now = time.time()
    existing_manual_hold = int(group_state.get("manual_hold_until", 0))
    if existing_manual_hold and now >= existing_manual_hold:
        if int(group_state.get("manual_preference_expired_for", 0)) != existing_manual_hold:
            record_manual_preference_event(
                state, group_name, "MANUAL_PREFERENCE_EXPIRED",
                "manual_preference_elapsed", now,
            )
            group_state["manual_preference_expired_for"] = existing_manual_hold
        group_state["manual_hold_until"] = 0
    maximum_manual_hold = int(now + MANUAL_HOLD_SECONDS)
    if int(group_state.get("manual_hold_until", 0)) > maximum_manual_hold:
        group_state["manual_hold_until"] = maximum_manual_hold
        group_state["manual_hold_capped_at"] = int(now)
        log("%s: capped legacy manual preference to 60 minutes" % group_name)
    if not candidates:
        if int(group_state.get("manual_hold_until", 0)) > now:
            record_manual_preference_event(
                state, group_name, "MANUAL_PREFERENCE_INTERRUPTED",
                "safe_candidate_unavailable", now,
            )
        group_state["manual_hold_until"] = 0
        group_state["dynamic_no_candidate"] = True
        log("%s: no safe candidate; fail closed" % group_name)
        return
    if current not in candidates:
        if int(group_state.get("manual_hold_until", 0)) > now:
            record_manual_preference_event(
                state, group_name, "MANUAL_PREFERENCE_INTERRUPTED",
                "selected_node_not_in_candidates", now,
            )
        group_state["manual_hold_until"] = 0
        log("%s: current selection is unavailable: %r" % (group_name, current))
        mature = [name for name in candidates if eligible_for_optimization(state["nodes"].get(name, {}))]
        target = min(
            mature,
            key=lambda name: float(state["nodes"].get(name, {}).get("score", 1000000.0)),
        ) if mature else None
        if target and business_preflight(group_name, target, state, dry_run):
            select_node(group_name, target, dry_run)
            timeline_add(group_name, {"t": round(now, 1), "ms": None, "node": target, "kind": "switch", "reason": "removed"})
            group_state.update({
                "last_router_selection": target,
                "last_seen": target,
                "last_switch_at": int(now),
                "handover_old_node": current,
                "handover_new_node": target,
                "handover_grace_until": int(now + 300),
                "recovery_observe_until": int(now + 360),
                "better_candidate": None,
                "better_streak": 0,
                "dynamic_no_candidate": False,
            })
            log("%s: current removed; selected mature backup %s without closing old connections" % (
                group_name, target,
            ))
            logging_setup.write_event("current_removed_switch", group=group_name, **{"from": current}, to=target)
            return
        group_state["dynamic_no_candidate"] = True
        log("%s: current removed but no mature backup passed preflight" % group_name)
        return

    previous_seen = group_state.get("last_seen")
    router_choice = group_state.get("last_router_selection")

    if previous_seen and current != previous_seen and current != router_choice:
        group_state["manual_hold_until"] = int(now + MANUAL_HOLD_SECONDS)
        group_state["manual_preference_expired_for"] = 0
        group_state["last_seen"] = current
        group_state["better_candidate"] = None
        group_state["better_streak"] = 0
        record_manual_preference_event(
            state, group_name, "MANUAL_PREFERENCE_STARTED",
            "manual_selection_detected", now,
        )
        log("%s: manual preference detected; performance optimization paused for 60 minutes; safety failover remains active" % group_name)

    current_stats = state["nodes"].get(current, {})
    current_failures = int(current_stats.get("effective_failure_streak", current_stats.get("failure_streak", 0)))
    current_quarantined = is_quarantined(current_stats, now)
    if current_failures >= FAILURES_BEFORE_SWITCH or current_quarantined:
        if int(group_state.get("manual_hold_until", 0)) > now:
            record_manual_preference_event(
                state, group_name, "MANUAL_PREFERENCE_INTERRUPTED",
                "current_quarantined" if current_quarantined else "confirmed_current_failure", now,
            )
        group_state["manual_hold_until"] = 0
        group_state["better_candidate"] = None
        group_state["better_streak"] = 0
        storm = health_model.count_recent(
            group_state.get("failover_times"), now, FAILOVER_STORM_WINDOW_SECONDS
        ) >= FAILOVER_STORM_THRESHOLD
        if storm and now - int(group_state.get("failover_storm_event_at", 0)) >= FAILOVER_STORM_WINDOW_SECONDS:
            group_state["failover_storm_event_at"] = int(now)
            record_runtime_event(state, "FAILOVER_STORM", "repeated_failovers", now, group_name)
            log("%s: failover storm; every failover target now requires a full business preflight" % group_name)
        remaining = list(candidates)
        target = None
        while remaining:
            candidate = best_failover(remaining, state["nodes"], current)
            if not candidate:
                break
            if business_preflight(group_name, candidate, state, dry_run, now=now, allow_fresh_skip=not storm):
                target = candidate
                break
            remaining.remove(candidate)
            log("%s: skip %s; business preflight failed" % (group_name, candidate))
        if target:
            reason = "confirmed_failure" if current_failures >= FAILURES_BEFORE_SWITCH else "quarantined"
            log("%s: FAILOVER %s -> %s (%s)" % (
                group_name, current, target,
                "%d consecutive failures" % current_failures if reason == "confirmed_failure"
                else "quarantined: %d failures within %d minutes" % (
                    len(current_stats.get("recent_failures", [])), QUARANTINE_WINDOW_SECONDS // 60),
            ))
            select_node(group_name, target, dry_run)
            timeline_add(group_name, {"t": round(now, 1), "ms": None, "node": target, "kind": "switch", "reason": "failover"})
            closed = 0 if dry_run else close_old_connections(group_name, current, connections)
            log("%s: closed %d stale connections" % (group_name, closed))
            logging_setup.write_event(
                "failover", group=group_name, **{"from": current}, to=target,
                reason=reason, failures=current_failures, closed_connections=closed, storm=storm,
                lane=(group_state.get("last_confirm") or {}).get("lane", "cycle"),
                detect_seconds=max(0, int(now) - int(current_stats.get("first_failure_at", now))),
                recent_probes=recent_points(group_name))
            group_state["failover_times"] = health_model.bounded_append(
                group_state.get("failover_times"), int(now), SWITCH_HISTORY_LIMIT)
            group_state["last_failover_detect_seconds"] = max(
                0, int(now) - int(current_stats.get("first_failure_at", now)))
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
            group_state["manual_hold_until"] = 0
            group_state["dynamic_no_candidate"] = False
            return
        log("%s: current node failed but no tested backup is available" % group_name)
        group_state["dynamic_no_candidate"] = True
        return

    active = active_connections(group_name, connections)

    if now < float(group_state.get("manual_hold_until", 0)):
        group_state["last_seen"] = current
        log_routine((group_name, "keep"), "manual:%s" % current,
                    "%s: keep %s; manual preference pauses performance optimization; safety failover remains active" % (
                        group_name, current))
        return

    last_switch = float(group_state.get("last_switch_at", 0))
    if not group_state.get("recovery_mode") and now - last_switch < PERFORMANCE_COOLDOWN_SECONDS:
        group_state["last_seen"] = current
        log_routine((group_name, "keep"), "cooldown:%s" % current,
                    "%s: keep %s; performance cooldown is active" % (group_name, current))
        return

    eligible = [name for name in candidates if eligible_for_optimization(state["nodes"].get(name, {}))]
    if current not in eligible or len(eligible) < 2:
        group_state["last_seen"] = current
        log_routine((group_name, "keep"), "collecting:%s" % current,
                    "%s: keep %s; collecting weighted history" % (group_name, current))
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
        # The leader flips on jitter without changing the decision, so only the current node counts.
        log_routine((group_name, "keep"), "steady:%s" % current,
                    "%s: keep %s; weighted score %.0f (best %.0f)" % (group_name, current, current_score, leader_score))
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
    timeline_add(group_name, {"t": round(now, 1), "ms": None, "node": leader, "kind": "switch", "reason": "optimize"})
    group_state["last_router_selection"] = leader
    group_state["last_seen"] = leader
    group_state["last_switch_at"] = int(now)
    group_state["recovery_mode"] = False
    group_state["performance_switch_times"] = health_model.bounded_append(
        group_state.get("performance_switch_times"), int(now), SWITCH_HISTORY_LIMIT)
    group_state["handover_old_node"] = current
    group_state["handover_new_node"] = leader
    group_state["handover_grace_until"] = int(now + 300)
    group_state["recovery_observe_until"] = int(now + 360)
    group_state["better_candidate"] = None
    group_state["better_streak"] = 0
    logging_setup.write_event(
        "optimize", group=group_name, **{"from": current}, to=leader,
        score_from=round(current_score, 1), score_to=round(leader_score, 1),
        preserved_connections=len(active))
    if active:
        log("%s: lossless recovery preserved %d existing connections on %s" % (
            group_name, len(active), current
        ))


def detect_cycle_resume(state, cycle_started_at):
    """Detect a system sleep before any probe of this cycle is recorded."""
    wall_now, mono_now = time.time(), time.monotonic()
    previous_update = int(state.get("updated_at", 0))
    if RUNTIME["last_cycle_wall"] is None:
        # Fresh process: only the persisted completion time is available.
        slept = bool(previous_update) and cycle_started_at - previous_update > PROBE_INTERVAL_SECONDS * 3
        gap = max(0, cycle_started_at - previous_update) if previous_update else 0
    else:
        slept, gap = health_model.detect_resume(
            RUNTIME["last_cycle_wall"], RUNTIME["last_cycle_mono"], wall_now, mono_now,
            PROBE_INTERVAL_SECONDS, RUNTIME["last_duration_s"],
        )
    RUNTIME["last_cycle_wall"], RUNTIME["last_cycle_mono"] = wall_now, mono_now
    if previous_update:
        state["last_cycle_gap_seconds"] = max(0, cycle_started_at - previous_update)
    if slept:
        state["last_resume_at"] = cycle_started_at
        state["last_sleep_gap_seconds"] = gap
        log("resume detected after %d seconds without probes" % gap)
        logging_setup.write_event("resume", gap_seconds=gap)
    return slept


def current_for_group(proxy_data, group_name, candidates):
    current = (proxy_data.get(group_name) or {}).get("now")
    return current if current in candidates else candidates[0]


def record_probe_results(state, targets, base_results, observed_at, record_failures, transient=()):
    """Write base probe samples. Failures are dropped when record_failures is False;
    failures of nodes in `transient` (re-probed fine this cycle) skip the quarantine window."""
    for name in targets:
        delay = base_results.get(name)
        if delay is None and not record_failures:
            continue
        node_state = state["nodes"].setdefault(name, {})
        update_node_stats(node_state, delay, observed_at, quarantine=name not in transient)
        node_state["last_probe_at"] = observed_at


def business_verdict_for(current_ok, standby, standby_result, standby_probed):
    if current_ok:
        return "ok"
    if standby and standby_probed:
        return "node" if standby_result is not None else "target"
    return "unknown"


def update_local_network_state(state, local_ok, now):
    previous = state.get("local_network_ok")
    state["local_network_ok"] = bool(local_ok)
    if previous is not False and not local_ok:
        record_runtime_event(state, "LOCAL_NETWORK_OFFLINE", "direct_probe_failed", now,
                             from_state="online", to_state="offline")
        log("local network check failed; failure accounting and switching paused")
    elif previous is False and local_ok:
        record_runtime_event(state, "LOCAL_NETWORK_RECOVERED", "direct_probe_succeeded", now,
                             severity="info", from_state="offline", to_state="online")
        log("local network recovered")


def finish_cycle(state, proxy_data, connections, cycle_clock, cycle_started_at):
    completed_at = int(time.time())
    duration = time.perf_counter() - cycle_clock
    state["last_cycle_started_at"] = cycle_started_at
    state["updated_at"] = completed_at
    state["last_cycle_duration_ms"] = int(round(duration * 1000))
    state["cycle_count"] = int(state.get("cycle_count", 0)) + 1
    record_memory_sample(state, completed_at)
    state["cycle_durations_ms"] = health_model.bounded_append(
        state.get("cycle_durations_ms"), state["last_cycle_duration_ms"], CYCLE_DURATION_SAMPLES)
    RUNTIME["last_duration_s"] = duration
    publish_state(state, proxy_data, connections, completed_at)


def publish_state(state, proxy_data, connections, now):
    snapshots = build_status_snapshots(state, proxy_data, connections, now=int(now))
    save_state(state)
    update_dashboard_cache(snapshots)
    sync_state_events(state)
    RUNTIME.update({
        "last_state": state, "last_proxy_data": proxy_data, "last_connections": connections,
        "last_snapshots": snapshots,
    })


def refresh_cached_snapshot(proxy_data=None, now=None):
    """Patch the last full snapshot with the fast lane's live fields and re-encode it.

    Everything else (decisions, node table, events) stays as the last full cycle left it;
    the next full cycle rebuilds the whole snapshot within PROBE_INTERVAL_SECONDS.
    """
    snapshots = RUNTIME.get("last_snapshots")
    if snapshots is None:
        return
    now = int(time.time()) if now is None else int(now)
    last_cycle = (RUNTIME.get("last_state") or {}).get("updated_at")
    snapshot_id = "snapshot-%d-%d" % (now, int(last_cycle or 0))
    last_probe_at = RUNTIME.get("last_probe_at")
    current_mb = footprint_now()
    rss_peak_mb = memory_megabytes()
    peak_mb = RUNTIME.get("memory_footprint_peak_mb") or rss_peak_mb
    v1, legacy = snapshots["v1"], snapshots["legacy"]
    snapshots["snapshot_id"] = snapshot_id
    v1["generated_at"], v1["generated_at_iso"] = now, iso_timestamp(now)
    v1["diagnostics"]["snapshot_id"] = snapshot_id
    legacy["snapshot_id"] = snapshot_id
    v1["service"]["uptime_seconds"] = max(0, now - SERVICE_STARTED_AT)
    for service in (v1["service"], legacy["service"]):
        service["last_probe_at"] = last_probe_at or service.get("last_probe_at")
        service["memory_mb"] = rss_peak_mb
        service["memory_peak_mb"] = peak_mb
        service["memory_current_mb"] = current_mb
    for group in v1["groups"]:
        group["recent_probes"] = public_timeline(group["name"], now, V1_TIMELINE_WINDOW_SECONDS)
    for group in legacy["groups"]:
        group["timeline"] = public_timeline(group["name"], now)
    update_dashboard_cache(snapshots)


def run_fast_tick(dry_run=False):
    """Probe only the current node of each group between full cycles.

    Returns 'skip' | 'resume' | 'controller_error' | 'ok' | 'transient' | 'local_offline' | 'failover'.
    A confirmed failure fails over right away through the normal evaluate_group
    path, so every same-region, fail-closed and preflight rule still applies.
    """
    if RUNTIME.get("last_state") is None:
        return "skip"
    wall, mono = time.time(), time.monotonic()
    previous_wall, previous_mono = RUNTIME.get("last_tick_wall"), RUNTIME.get("last_tick_mono")
    RUNTIME["last_tick_wall"], RUNTIME["last_tick_mono"] = wall, mono
    if previous_wall is not None and (wall - previous_wall) - (mono - previous_mono) > 15:
        return "resume"
    try:
        proxy_data = (api_request("GET", "/proxies") or {}).get("proxies") or {}
    except Exception:
        return "controller_error"
    base_state = RUNTIME["last_state"]
    plan = {}
    for policy in POLICIES:
        group_name = policy["group_name"]
        candidates = candidate_registry.routing_candidates(POLICY_CONFIG, policy, base_state)
        current = (proxy_data.get(group_name) or {}).get("now")
        if candidates and current in candidates:
            plan[group_name] = (current, candidates)
    results = run_probe_jobs([
        (group_name, current, FAST_PROBE_URL, FAST_PROBE_TIMEOUT_MS)
        for group_name, (current, _candidates) in plan.items()
    ])
    now = time.time()
    RUNTIME["last_probe_at"] = int(now)
    for group_name, (current, _candidates) in plan.items():
        timeline_add(group_name, {"t": round(now, 1), "ms": results.get(group_name), "node": current, "kind": "probe"})
    failed = [group_name for group_name in plan if results.get(group_name) is None]
    if not failed:
        refresh_cached_snapshot(proxy_data)
        return "ok"
    cycle_cache = {}
    verdicts = {group_name: confirm_current_failure(plan[group_name][0], cycle_cache) for group_name in failed}
    if cycle_cache.get("local_ok") is False:
        state = load_state()
        update_local_network_state(state, False, int(now))
        publish_state(state, proxy_data, RUNTIME.get("last_connections") or [], time.time())
        return "local_offline"
    confirmed = [group_name for group_name in failed if verdicts[group_name] == "confirmed"]
    if not confirmed:
        refresh_cached_snapshot(proxy_data)
        return "transient"
    state = load_state()
    try:
        connections = (api_request("GET", "/connections") or {}).get("connections") or []
    except Exception:
        connections = []
    for group_name in confirmed:
        current, candidates = plan[group_name]
        node_state = state["nodes"].setdefault(current, {})
        update_node_stats(node_state, None, int(now))
        node_state["last_probe_at"] = int(now)
        node_state["effective_failure_streak"] = max(
            int(node_state.get("effective_failure_streak", 0)), FAILURES_BEFORE_SWITCH)
        state["groups"].setdefault(group_name, {})["last_confirm"] = {
            "verdict": "confirmed", "at": int(now), "lane": "fast",
        }
        log("%s: fast lane confirmed %s is down" % (group_name, current))
        evaluate_group(group_name, candidates, proxy_data, connections, state, dry_run)
    update_local_network_state(state, True, int(now))
    publish_state(state, proxy_data, connections, time.time())
    return "failover"


def run_cycle(dry_run=False):
    cycle_clock = time.perf_counter()
    cycle_started_at = int(time.time())
    state = load_state()
    merge_legacy_cycle_count(state)
    resume_cycle = detect_cycle_resume(state, cycle_started_at)
    try:
        proxy_response = api_request("GET", "/proxies") or {}
    except Exception:
        state["controller_connected"] = False
        candidate_registry.reconcile(POLICY_CONFIG, state, {}, cycle_started_at, controller_ok=False)
        snapshots = build_status_snapshots(state, {}, [], now=int(time.time()))
        save_state(state)
        update_dashboard_cache(snapshots)
        raise
    proxy_data = proxy_response.get("proxies") or {}
    state["controller_connected"] = True
    candidate_registry.reconcile(POLICY_CONFIG, state, proxy_data, cycle_started_at)
    try:
        connection_response = api_request("GET", "/connections") or {}
        connections = connection_response.get("connections") or []
    except Exception:
        connections = []
    targets = choose_probe_targets(state, proxy_data)

    # 1) One parallel batch: base probes, due business probes (current + hot standby
    #    on the same URL) and warm-up business probes.
    jobs = [(("base", name), name, TEST_URL, PROBE_TIMEOUT_MS) for name in targets]
    for group_name, candidates in GROUPS.items():
        # Same lightweight probe the fast lane uses, so the live strip stays continuous.
        jobs.append((("fast", group_name), current_for_group(proxy_data, group_name, candidates),
                     FAST_PROBE_URL, FAST_PROBE_TIMEOUT_MS))
        # The hot standby gets the same lightweight probe so the dashboard can compare
        # both lines on one scale. Display only: never recorded into node statistics.
        standby = state["groups"].get(group_name, {}).get("hot_standby")
        if standby and standby != current_for_group(proxy_data, group_name, candidates):
            jobs.append((("fast_standby", group_name), standby, FAST_PROBE_URL, FAST_PROBE_TIMEOUT_MS))
    business_plan = {}
    scheduled = set()
    for group_name, candidates in GROUPS.items():
        current = current_for_group(proxy_data, group_name, candidates)
        group_state = state["groups"].setdefault(group_name, {})
        current_state = state["nodes"].get(current, {})
        business_due = (
            cycle_started_at - int(group_state.get("last_business_probe_at", 0)) >= BUSINESS_PROBE_INTERVAL_SECONDS
            or int(current_state.get("effective_failure_streak", 0)) > 0
        )
        urls = BUSINESS_TEST_URLS.get(group_name, [])
        if not (business_due and urls):
            continue
        cursor = int(group_state.get("business_probe_cursor", 0))
        url = urls[cursor % len(urls)]
        group_state["business_probe_cursor"] = (cursor + 1) % len(urls)
        group_state["last_business_probe_at"] = cycle_started_at
        standby = group_state.get("hot_standby")
        jobs.append((("biz", group_name, current), current, url, BUSINESS_PROBE_TIMEOUT_MS))
        scheduled.add((group_name, current))
        if standby and standby != current:
            jobs.append((("biz", group_name, standby), standby, url, BUSINESS_PROBE_TIMEOUT_MS))
            scheduled.add((group_name, standby))
        business_plan[group_name] = (current, url, standby if standby != current else None)
    for policy, name in candidate_registry.warmup_probe_targets(POLICY_CONFIG, state):
        group_name = policy["group_name"]
        urls = policy.get("business_test_urls", [])
        node_state = state["nodes"].get(name, {})
        if urls and not node_state.get("business_successes") and (group_name, name) not in scheduled:
            jobs.append((("biz", group_name, name), name, urls[0], BUSINESS_PROBE_TIMEOUT_MS))
            scheduled.add((group_name, name))
    results = run_probe_jobs(jobs)

    # 2) Retry every failed business probe once, all in parallel.
    retry = run_probe_jobs([job for job in jobs if job[0][0] == "biz" and results.get(job[0]) is None])
    business = {}
    for key, name, url, _timeout in jobs:
        if key[0] != "biz":
            continue
        delay = results.get(key)
        business[key] = {"name": name, "url": url, "delay": delay if delay is not None else retry.get(key)}
    base_results = {name: results.get(("base", name)) for name in targets}
    probed_at = time.time()
    RUNTIME["last_probe_at"] = int(probed_at)
    for group_name, candidates in GROUPS.items():
        timeline_add(group_name, {
            "t": round(probed_at, 1), "ms": results.get(("fast", group_name)),
            "node": current_for_group(proxy_data, group_name, candidates), "kind": "probe",
        })
        standby = state["groups"].get(group_name, {}).get("hot_standby")
        if ("fast_standby", group_name) in results:
            timeline_add(group_name, {
                "t": round(probed_at, 1), "ms": results.get(("fast_standby", group_name)),
                "node": standby, "kind": "standby",
            })

    # 3) Classify business failures: node-side vs site-side.
    verdicts = {}
    for group_name, (current, url, standby) in business_plan.items():
        current_ok = business[("biz", group_name, current)]["delay"] is not None
        standby_key = ("biz", group_name, standby) if standby else None
        verdicts[group_name] = business_verdict_for(
            current_ok, standby,
            business[standby_key]["delay"] if standby_key in business else None,
            standby_key in business,
        )

    # 4) Confirm a failed current node inside this cycle, with a local-network guard.
    #    When every probed node failed at once (both regions), the cause is local or
    #    controller-wide; the network may already be back by the time we check, so
    #    those samples are never charged to nodes.
    cycle_cache = {}
    confirmations = {}
    alive_elsewhere = set()   # base probe failed, but another probe through the node succeeded
    blackout = len(targets) >= MASS_FAILURE_MIN_TARGETS and all(
        base_results.get(name) is None for name in targets)
    if blackout:
        ensure_local_check(cycle_cache)
        log("all %d probes failed at once; treating the cycle as a local outage" % len(targets))
    for group_name, candidates in GROUPS.items():
        current = current_for_group(proxy_data, group_name, candidates)
        if base_results.get(current) is not None:
            continue
        if verdicts.get(group_name) == "ok" or results.get(("fast", group_name)) is not None:
            alive_elsewhere.add(group_name)
            continue
        if resume_cycle or blackout:
            ensure_local_check(cycle_cache)
            confirmations[group_name] = "resume" if resume_cycle else "blackout"
            continue
        if cycle_cache.get("local_ok") is False:
            confirmations[group_name] = "local_offline"
            continue
        confirmations[group_name] = confirm_current_failure(current, cycle_cache)
        state["groups"].setdefault(group_name, {})["last_confirm"] = {
            "verdict": confirmations[group_name], "at": cycle_started_at,
        }
    local_offline = cycle_cache.get("local_ok") is False
    record_failures = not (local_offline or resume_cycle or blackout)
    update_local_network_state(state, not local_offline, cycle_started_at)

    # 5) Record samples.
    transient = {
        current_for_group(proxy_data, group_name, candidates)
        for group_name, candidates in GROUPS.items()
        if confirmations.get(group_name) == "transient" or group_name in alive_elsewhere
    }
    record_probe_results(state, targets, base_results, cycle_started_at, record_failures, transient)
    for (kind, group_name, name), result in business.items():
        success = result["delay"] is not None
        if success or record_failures:
            record_business_result(state["nodes"].setdefault(name, {}), success, cycle_started_at)
    for group_name, verdict in verdicts.items():
        group_state = state["groups"].setdefault(group_name, {})
        _current, url, _standby = business_plan[group_name]
        marks = dict(group_state.get("business_target_down") or {})
        if verdict == "ok":
            marks.pop(url, None)
        elif verdict == "target" and record_failures:
            if int(marks.get(url, 0)) <= cycle_started_at:
                record_runtime_event(state, "BUSINESS_TARGET_UNREACHABLE", "target_side_failure",
                                     cycle_started_at, group_name)
                log("%s: business target unreachable through current and standby; not a node failure" % group_name)
            marks[url] = cycle_started_at + BUSINESS_TARGET_DOWN_SECONDS
        group_state["business_target_down"] = {
            key: int(value) for key, value in marks.items() if int(value) > cycle_started_at
        }
    for group_name, candidates in GROUPS.items():
        current = current_for_group(proxy_data, group_name, candidates)
        node_state = state["nodes"].setdefault(current, {})
        # A blip that the confirmation probes disproved is not a failure of a live node.
        update_effective_health(
            node_state, base_results.get(current) is not None or current in transient, False, False,
            business_verdict=verdicts.get(group_name), record_failures=record_failures,
        )
        if confirmations.get(group_name) == "confirmed":
            node_state["effective_failure_streak"] = max(
                int(node_state.get("effective_failure_streak", 0)), FAILURES_BEFORE_SWITCH)

    summary = []
    for name in targets:
        stats = state["nodes"].get(name, {})
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
        for (_kind, group_name, _name), result in business.items()
    ]
    probe_line = "probe: " + " | ".join(summary) + (" | business " + " | ".join(business_summary) if business_summary else "")
    if "FAIL" in probe_line:
        log(probe_line)
    else:
        log_routine(("probe",), "ok", probe_line)

    for policy in POLICIES:
        group_name = policy["group_name"]
        candidates = candidate_registry.routing_candidates(POLICY_CONFIG, policy, state)
        if not candidates:
            state["groups"].setdefault(group_name, {})["dynamic_no_candidate"] = True
            continue
        state["groups"].setdefault(group_name, {})["dynamic_no_candidate"] = False
        if not record_failures:
            reason = ("local network offline" if local_offline
                      else "resumed from sleep" if resume_cycle else "all probes failed at once")
            log_routine((group_name, "paused"), reason, "%s: decisions paused this cycle (%s)" % (group_name, reason))
            continue
        evaluate_group(group_name, candidates, proxy_data, connections, state, dry_run)
    candidate_registry.refresh_lifecycles(POLICY_CONFIG, state, cycle_started_at)
    candidate_registry.purge_retired(POLICY_CONFIG, state, cycle_started_at)
    finish_cycle(state, proxy_data, connections, cycle_clock, cycle_started_at)


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
        print("another router instance is already running", file=sys.stderr)
        return 0

    # Only the lock holder touches the log files (rotation and migration are not multi-process safe).
    if args.daemon:
        try:
            logging_setup.configure(LOG_DIR)
        except OSError as error:
            logging_setup.configure(LOG_DIR, to_stdout=True)
            log_error("file logging unavailable, using stdout: %s" % error)
    else:
        logging_setup.configure(LOG_DIR, to_stdout=True)
    log("%s %s starting (pid %d)" % (SERVICE_NAME, read_service_version(), os.getpid()))
    try:
        sync_state_events(state_contract.migrate_state(load_state()), write=False)
    except Exception as error:
        log_warning("could not seed event log from state: %s" % error)

    dashboard_server = None
    if args.daemon:
        try:
            dashboard_server = start_dashboard()
        except Exception as error:
            log_error("dashboard failed to start: %s" % error, exc_info=True)

    deadline = time.monotonic()
    while True:
        try:
            run_cycle(dry_run=args.dry_run)
        except Exception as error:
            log_error("cycle failed: %s" % error, exc_info=not isinstance(error, (OSError, RuntimeError)))
        if not args.daemon:
            break
        # Fixed-rate full cycles every PROBE_INTERVAL_SECONDS; between them a fast
        # lane probes only the current nodes every FAST_PROBE_INTERVAL_SECONDS.
        deadline = health_model.next_deadline(deadline, PROBE_INTERVAL_SECONDS, time.monotonic())
        fast_deadline = time.monotonic() + FAST_PROBE_INTERVAL_SECONDS
        while True:
            WAKE_EVENT.wait(max(0.0, min(deadline, fast_deadline) - time.monotonic()))
            WAKE_EVENT.clear()
            if time.monotonic() >= deadline:
                break
            if time.monotonic() >= fast_deadline:
                try:
                    outcome = run_fast_tick(dry_run=args.dry_run)
                except Exception as error:
                    log_error("fast probe failed: %s" % error, exc_info=True)
                    outcome = None
                if outcome == "resume":
                    deadline = time.monotonic()
                    break
                fast_deadline = health_model.next_deadline(
                    fast_deadline, FAST_PROBE_INTERVAL_SECONDS, time.monotonic())
    if dashboard_server is not None:
        dashboard_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
