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
import tempfile
import threading
import time
from urllib.parse import quote


SOCKET_PATH = "/tmp/verge/verge-mihomo.sock"
BASE_DIR = "/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router"
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOCK_PATH = os.path.join(BASE_DIR, "router.lock")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")
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
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    state.setdefault("version", 1)
    state.setdefault("nodes", {})
    state.setdefault("groups", {})
    return state


def save_state(state):
    os.makedirs(BASE_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=BASE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, STATE_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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


def dashboard_payload(state=None, proxy_data=None, connections=None):
    if state is None:
        state = load_state()
    if proxy_data is None:
        try:
            proxy_response = api_request("GET", "/proxies") or {}
            proxy_data = proxy_response.get("proxies") or {}
        except Exception:
            proxy_data = {}
    if connections is None:
        try:
            connection_data = api_request("GET", "/connections") or {}
            connections = connection_data.get("connections") or []
        except Exception:
            connections = []

    groups = []
    for group_name, candidates in GROUPS.items():
        group_state = state.get("groups", {}).get(group_name, {})
        current = (proxy_data.get(group_name) or {}).get("now")
        active = sum(1 for connection in connections if group_name in (connection.get("chains") or []))
        current_stats = state.get("nodes", {}).get(current, {})
        if int(current_stats.get("effective_failure_streak", current_stats.get("failure_streak", 0))) >= FAILURES_BEFORE_SWITCH:
            decision = "故障切换准备"
        elif int(group_state.get("better_streak", 0)):
            decision = "候选确认 %d/%d" % (
                int(group_state.get("better_streak", 0)), PERFORMANCE_CONFIRMATIONS
            )
        elif time.time() < float(group_state.get("manual_hold_until", 0)):
            decision = "手动保持"
        elif active:
            decision = "无损运行"
        else:
            decision = "稳定观察"
        groups.append({
            "name": group_name,
            "current": current,
            "active_connections": active,
            "decision": decision,
            "better_candidate": group_state.get("better_candidate"),
            "better_streak": int(group_state.get("better_streak", 0)),
            "manual_hold_until": int(group_state.get("manual_hold_until", 0)),
            "last_switch_at": int(group_state.get("last_switch_at", 0)),
            "candidates": candidates,
        })

    nodes = {}
    for name, node_state in state.get("nodes", {}).items():
        nodes[name] = {
            "availability": round(float(node_state.get("availability_ewma", 0.0)), 4),
            "latency": round(float(node_state["latency_ewma"]), 1) if node_state.get("latency_ewma") is not None else None,
            "jitter": round(float(node_state.get("jitter_ewma", 0.0)), 1),
            "score": round(float(node_state.get("score", 1000000.0)), 1),
            "quality": quality_score(node_state),
            "last_delay": node_state.get("last_delay"),
            "last_success": bool(node_state.get("last_success")),
            "success_streak": int(node_state.get("success_streak", 0)),
            "failure_streak": int(node_state.get("failure_streak", 0)),
            "samples": int(node_state.get("samples", 0)),
            "short_availability": round(short_availability(node_state), 4),
            "long_availability": round(long_availability(node_state), 4),
            "quarantined": is_quarantined(node_state),
            "quarantine_until": int(node_state.get("quarantine_until", 0)),
            "business_last_success": node_state.get("business_last_success"),
        }

    return {
        "service": {
            "name": SERVICE_NAME,
            "status": "running",
            "started_at": SERVICE_STARTED_AT,
            "updated_at": int(state.get("updated_at", 0)),
            "memory_mb": memory_megabytes(),
            "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
            "last_cycle_gap_seconds": int(state.get("last_cycle_gap_seconds", 0)),
            "last_resume_at": int(state.get("last_resume_at", 0)),
            "last_sleep_gap_seconds": int(state.get("last_sleep_gap_seconds", 0)),
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
        "groups": groups,
        "nodes": nodes,
    }


def update_dashboard_cache(payload):
    global DASHBOARD_CACHE
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with DASHBOARD_CACHE_LOCK:
        DASHBOARD_CACHE = encoded


def read_dashboard_cache():
    with DASHBOARD_CACHE_LOCK:
        return DASHBOARD_CACHE


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
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
        if self.path.startswith("/api/status"):
            content = read_dashboard_cache()
            if content is None:
                content = json.dumps(dashboard_payload(), ensure_ascii=False).encode("utf-8")
                update_dashboard_cache(json.loads(content.decode("utf-8")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_error(404)

    def log_message(self, _format, *_args):
        return


def start_dashboard():
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
    select_node(group_name, leader, dry_run)
    group_state["last_router_selection"] = leader
    group_state["last_seen"] = leader
    group_state["last_switch_at"] = int(now)
    group_state["recovery_mode"] = False
    group_state["better_candidate"] = None
    group_state["better_streak"] = 0
    if active:
        log("%s: lossless recovery preserved %d existing connections on %s" % (
            group_name, len(active), current
        ))


def run_cycle(dry_run=False):
    cycle_started_at = int(time.time())
    proxy_response = api_request("GET", "/proxies") or {}
    proxy_data = proxy_response.get("proxies") or {}
    try:
        connection_response = api_request("GET", "/connections") or {}
        connections = connection_response.get("connections") or []
    except Exception:
        connections = []
    state = load_state()
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
    save_state(state)
    update_dashboard_cache(dashboard_payload(state, proxy_data, connections))


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
