"""诊断信息: one plain-text report the settings page copies to the clipboard. Read-only.

Written to answer "why did it switch": the switches of the last 7 days with what the probes
saw just before, next to the things that look like a node failure but are not (the Mac asleep,
the service restarting), and each node's health. Standard library only.

What goes in: versions, the lines and their state, switch / sleep / start events from
events.jsonl, node health from the status snapshot, the tail of router-error.log.
What never goes in: subscription addresses, server addresses, passwords, the sites the
connections go to. Home folder paths, IP addresses and URL queries are masked on the way out.
"""

import datetime
import gzip
import json
import os
import platform
import re
import time

DAYS = 7
MAX_SWITCHES = 120
MAX_SERVICE_EVENTS = 60
MAX_ERROR_LINES = 30
MAX_BYTES = 60 * 1024
NEAR_WAKE_SECONDS = 180       # a failover this soon after a wake-up or start is suspect

SWITCH_LABELS = {"failover": "故障切换", "optimize": "回优", "current_removed_switch": "节点移除",
                 "auto_lock_adopt": "切至家宽"}
SERVICE_LABELS = {"service_start": "服务启动", "resume": "休眠唤醒", "settings_changed": "设置变更",
                  "lines_rewritten": "专线重新写入", "rules_synced": "规则同步",
                  "auto_lock_locked": "锁定国家", "auto_lock_relocked": "改锁国家",
                  "region_guard_blocked": "同地区校验拦截"}
START_LABELS = {"boot": "开机", "restart": "重启", "crash": "异常后拉起", "first": "首次启动", "upgrade": "升级"}
_ARCHIVE = re.compile(r"^events-(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.jsonl(\.gz)?$")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}(?![\d.])")
# Compressed (has "::") or at least six groups, so a clock time such as 23:19:50 is left alone.
_IPV6 = re.compile(r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?"
                   r"|(?:[0-9A-Fa-f]{1,4}:){5,7}[0-9A-Fa-f]{1,4})(?![\w:])")
_URL_QUERY = re.compile(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*")


def scrub(text, home=None):
    """Mask what a paste into a chat should not carry."""
    home = home or os.path.expanduser("~")
    if home and home != "/":
        text = text.replace(home, "~")
    text = re.sub(r"/Users/[^/\s\"']+", "/Users/…", text)
    text = _URL_QUERY.sub(r"\1?…", text)
    text = _IPV4.sub(lambda m: m.group(0) if m.group(1) in ("127", "0") else "%s.%s.*.*" % (m.group(1), m.group(2)), text)
    return _IPV6.sub("[IPv6]", text)


def read_events(log_dir, since, now):
    """Records from events.jsonl and its daily archives, oldest first, state_event noise left out."""
    names = []
    try:
        listing = os.listdir(log_dir)
    except OSError:
        return []
    first_day = time.strftime("%Y-%m-%d", time.localtime(since - 86400))
    for name in listing:
        match = _ARCHIVE.match(name)
        if match and match.group(1) >= first_day:
            names.append((match.group(1), name))
    names.sort()
    names.append(("9999", "events.jsonl"))
    records = []
    for _day, name in names:
        path = os.path.join(log_dir, name)
        try:
            opener = gzip.open if name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"state_event"' in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and since <= float(record.get("unix") or 0) <= now + 60:
                        records.append(record)
        except (OSError, EOFError):
            continue
    records.sort(key=lambda record: record.get("unix") or 0)
    return records


def tail(path, lines):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line for line in data.splitlines() if line.strip()][-lines:]


def _clock(unix, with_date=True):
    if not unix:
        return "—"
    return time.strftime("%m-%d %H:%M:%S" if with_date else "%H:%M:%S", time.localtime(float(unix)))


def _span(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return "%d 秒" % seconds
    if seconds < 5400:
        return "%d 分" % round(seconds / 60.0)
    if seconds < 172800:
        return "%.1f 小时" % (seconds / 3600.0)
    return "%.1f 天" % (seconds / 86400.0)


def _percent(value):
    return "—" if value is None else "%d%%" % round(float(value) * 100)


def _probes(points):
    """The probe values just before a switch: latencies in ms, 失败 for a failed probe."""
    return " ".join("失败" if value is None else "%d" % round(value) for _at, value in (points or [])[-8:]) or "—"


def _switch_line(record, wake_times):
    kind = record.get("kind")
    at = float(record.get("unix") or 0)
    parts = ["%s %s" % (_clock(at), SWITCH_LABELS.get(kind, kind)),
             "%s: %s → %s" % (record.get("group"), record.get("from"), record.get("to"))]
    if kind == "failover":
        reason = {"confirmed_failure": "连续失败 %s 次" % record.get("failures"),
                  "quarantined": "节点已隔离"}.get(record.get("reason"), str(record.get("reason")))
        parts += [reason, "发现用时 %s 秒" % record.get("detect_seconds"),
                  "快速通道" if record.get("lane") == "fast" else "完整周期",
                  "断开连接 %s 个" % record.get("closed_connections"), "切换前探测 %s" % _probes(record.get("recent_probes"))]
        if record.get("storm"):
            parts.append("多节点同时失败")
        near = [at - wake for wake in wake_times if 0 <= at - wake <= NEAR_WAKE_SECONDS]
        if near:
            parts.append("距唤醒或启动 %d 秒" % min(near))
    elif kind == "optimize":
        if record.get("reason") == "sustained_slow":
            parts.append("持续变慢 %s 轮 · 延迟 %s → %s ms · 抖动 %s → %s ms" % (
                record.get("held_cycles"), record.get("latency_from"), record.get("latency_to"),
                record.get("jitter_from"), record.get("jitter_to")))
        parts += ["评分 %s → %s" % (record.get("score_from"), record.get("score_to")),
                  "保留连接 %s 个" % record.get("preserved_connections")]
    elif kind == "auto_lock_adopt":
        parts.append("原因 %s" % record.get("reason"))
    return " · ".join(parts)


def _service_line(record):
    kind = record.get("kind")
    head = "%s %s" % (_clock(record.get("unix")), SERVICE_LABELS.get(kind, kind))
    if kind == "service_start":
        return "%s · %s · 中断 %s" % (head, START_LABELS.get(record.get("start"), record.get("start")),
                                    _span(record.get("gap_seconds") or 0))
    if kind == "resume":
        return "%s · 休眠 %s · 期间短暂唤醒 %s 次" % (head, _span(record.get("episode_seconds") or record.get("gap_seconds") or 0),
                                             record.get("brief_wakes") or 0)
    if kind == "settings_changed":
        result = record.get("result") or {}
        return "%s · %s" % (head, "写入 Clash：%s，%s 条规则" % ("、".join(result.get("groups") or []) or "撤销", result.get("rule_count", 0))
                            if result.get("clash_change") else "未改动 Clash")
    if kind == "lines_rewritten":
        return "%s · 第 %s 次（一小时内）" % (head, (record.get("status") or {}).get("repairs"))
    if kind == "rules_synced":
        return "%s · %s%s" % (head, "成功" if record.get("ok") else "未成功", "，规则有变化" if record.get("changed") else "")
    if kind in ("auto_lock_locked", "auto_lock_relocked"):
        return "%s · %s → %s（%s 个家宽候选）" % (head, record.get("group"), record.get("country"), record.get("candidates"))
    return "%s · %s" % (head, json.dumps({k: v for k, v in record.items() if k not in ("kind", "ts", "unix")},
                                         ensure_ascii=False, sort_keys=True)[:160])


def build(status, settings, core_version, log_dir, now=None, home=None, system=None):
    """The report as text. `status` is the /api/status snapshot, `settings` the /api/settings one;
    either may be None when it could not be read."""
    now = time.time() if now is None else now
    status = status or {}
    settings = settings or {}
    service = status.get("service") or {}
    since = now - DAYS * 86400
    events = read_events(log_dir, since, now)
    switches = [record for record in events if record.get("kind") in SWITCH_LABELS]
    wakes = [record for record in events if record.get("kind") in ("resume", "service_start")]
    wake_times = [float(record.get("unix") or 0) for record in wakes]
    out = []

    offset = datetime.datetime.fromtimestamp(now).astimezone().strftime("%z")
    out.append("稳航诊断信息 · 生成于 %s（本机时间 UTC%s:%s）" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), offset[:3], offset[3:]))
    start = service.get("last_start") or {}
    out.append("版本 %s · 已运行 %s · 上次启动 %s（%s，中断 %s）" % (
        service.get("version") or "未知", _span(now - service["started_at"]) if service.get("started_at") else "未知",
        _clock(service.get("started_at")), START_LABELS.get(start.get("kind"), start.get("kind") or "未知"),
        _span(start.get("gap_seconds") or 0)))
    out.append("系统 %s · Python %s · Clash 内核 %s · TUN %s · IPv6 %s · 控制接口%s" % (
        system or ("macOS %s" % platform.mac_ver()[0] if platform.mac_ver()[0] else platform.platform()),
        platform.python_version(), core_version or "未知",
        {True: "开启", False: "关闭"}.get(settings.get("tun"), "未知"),
        {True: "开启", False: "关闭"}.get(settings.get("ipv6"), "未知"),
        "已连接" if service.get("controller_connected") else "未连接"))
    out.append("内存 当前 %s MB · 峰值 %s MB · 检测 %s 轮 · 状态 %s" % (
        service.get("memory_current_mb", "—"), service.get("memory_peak_mb", "—"), service.get("cycle_count", "—"),
        service.get("state_title") or service.get("state_code") or "—"))
    if settings.get("controller_error"):
        out.append("读取 Clash 失败：%s" % settings["controller_error"])
    if settings.get("clash") and not settings["clash"].get("ok"):
        out.append("无法定位 Clash Verge 的文件：%s" % settings["clash"].get("error"))

    out += ["", "[专线]"]
    line = settings.get("ai_line") or {}
    applied = settings.get("applied") or {}
    labels = {row.get("code"): row.get("label") for row in settings.get("countries") or []}
    if line.get("enabled"):
        out.append("AI 出口：%s（分组「%s」）· %s" % (
            labels.get(line.get("country"), line.get("country")), line.get("group_name"),
            "已写入 %s 条规则，写入于 %s" % (settings.get("applied_rule_count"), _clock(applied.get("at")))
            if line.get("group_name") in (applied.get("groups") or []) else "尚未写入 Clash"))
        out.append("换线方式：%s" % (
            "节点故障时切换；持续 10 分钟以上明显慢于备用节点时更换，24 小时内最多 3 次"
            if line.get("slow_exit", True) else "只在节点故障时切换（已关闭“节点变慢时更换”）"))
    else:
        out.append("AI 出口：未启用")
    managed = settings.get("managed_lines") or []
    if managed:
        out.append("其他家宽专线：%s" % "、".join("%s（%s）" % (row.get("group_name"), labels.get(row.get("country"), row.get("country")))
                                            for row in managed))
    if applied.get("dropped"):
        out.append("内核缺少数据库而跳过的规则：%s" % "、".join(",".join(item) for item in applied["dropped"]))
    watch = settings.get("line_status") or {}
    written = bool(applied.get("groups"))
    out.append("运行核对：%s%s%s" % (
        {"ok": "与写入内容一致", "missing": "未生效", "unknown": "连不上 Clash",
         "off": "尚未核对（服务启动后约 20 秒开始）" if written else "未写入专线"}.get(watch.get("state"), "未知"),
        "（%s）" % watch["problem"] if watch.get("problem") else "",
        " · 最近一次自动重新写入 %s" % _clock(watch["repaired_at"]) if watch.get("repaired_at") else ""))
    if watch.get("error"):
        out.append("重新写入未成功：%s" % watch["error"])
    source = settings.get("rules_source") or {}
    out.append("规则同步：上次成功 %s · 上次检查 %s · 连续失败 %s 次%s" % (
        _clock(source.get("synced_at")), _clock(source.get("checked_at")), source.get("failures") or 0,
        " · %s" % source["last_error"] if source.get("last_error") else ""))
    migration = settings.get("migration")
    if migration:
        out.append("旧版线路尚未升级（来自 %s）" % migration.get("from"))

    out += ["", "[线路]"]
    for group in status.get("groups") or []:
        lock = group.get("auto_lock") or {}
        metrics = group.get("metrics") or {}
        out.append("%s%s · 锁定%s · %s 个家宽候选 · 当前 %s · 热备 %s" % (
            group.get("name"), "（AI 专线，只在故障时切换）" if group.get("ai_line") else "",
            lock.get("country_label") or group.get("region_label") or "?", lock.get("candidates", len(group.get("candidates") or [])),
            group.get("current"), group.get("hot_standby") or "无"))
        out.append("  状态 %s：%s · 近 24 小时 故障切换 %s · 回优 %s · 节点移除 %s · 切至家宽 %s · 活跃连接 %s" % (
            group.get("decision_code"), group.get("decision"), metrics.get("failovers_24h", "—"),
            metrics.get("performance_switches_24h", "—"), metrics.get("removal_switches_24h", "—"),
            metrics.get("adopt_switches_24h", "—"), group.get("active_connections", "—")))
    idle = service.get("auto_lock_idle") or {}
    for name, item in sorted(idle.items()):
        out.append("%s · 未接管（%s）" % (name, item.get("status")))
    if not (status.get("groups") or idle):
        out.append("没有接管的线路")

    out += ["", "[近 %d 天概况]" % DAYS]
    days = {}
    for record in events:
        day = time.strftime("%m-%d", time.localtime(float(record.get("unix") or 0)))
        bucket = days.setdefault(day, {})
        bucket[record.get("kind")] = bucket.get(record.get("kind"), 0) + 1
    for day in sorted(days):
        bucket = days[day]
        out.append("%s  故障切换 %d · 回优 %d · 节点移除 %d · 切至家宽 %d · 休眠唤醒 %d · 服务启动 %d · 专线重新写入 %d" % (
            day, bucket.get("failover", 0), bucket.get("optimize", 0), bucket.get("current_removed_switch", 0),
            bucket.get("auto_lock_adopt", 0), bucket.get("resume", 0), bucket.get("service_start", 0),
            bucket.get("lines_rewritten", 0)))
    if not days:
        out.append("没有记录")
    failovers = [record for record in switches if record.get("kind") == "failover"]
    if failovers:
        near = [record for record in failovers if any(
            0 <= float(record.get("unix") or 0) - wake <= NEAR_WAKE_SECONDS for wake in wake_times)]
        storms = [record for record in failovers if record.get("storm")]
        out.append("故障切换共 %d 次：唤醒或启动后 %d 秒内 %d 次 · 多节点同时失败 %d 次 · 快速通道发现 %d 次" % (
            len(failovers), NEAR_WAKE_SECONDS, len(near), len(storms),
            len([record for record in failovers if record.get("lane") == "fast"])))
        left = {}
        for record in failovers:
            key = (record.get("group"), record.get("from"))
            left[key] = left.get(key, 0) + 1
        out.append("被切走的节点：" + "；".join("%s %d 次" % (node, count) for (_group, node), count in
                                           sorted(left.items(), key=lambda item: -item[1])[:12]))

    out += ["", "[切换记录]（最新在前，最多 %d 条）" % MAX_SWITCHES]
    out += [_switch_line(record, wake_times) for record in reversed(switches[-MAX_SWITCHES:])] or ["没有记录"]

    out += ["", "[服务与设置]（最新在前，最多 %d 条）" % MAX_SERVICE_EVENTS]
    others = [record for record in events if record.get("kind") not in SWITCH_LABELS]
    out += [_service_line(record) for record in reversed(others[-MAX_SERVICE_EVENTS:])] or ["没有记录"]

    out += ["", "[节点]", "名称 | 地区 | 状态 | 可用率 短期/长期 | 延迟 | 抖动 | 评分 | 样本 | 连续成功/失败 | 隔离到"]
    nodes = status.get("nodes") or {}
    for name in sorted(nodes, key=lambda key: (nodes[key].get("region_label") or "", -float(nodes[key].get("quality") or 0))):
        node = nodes[name]
        out.append("%s | %s | %s | %s/%s | %s | %s | %s | %s | %s/%s | %s" % (
            name, node.get("region_label") or "—", node.get("lifecycle_title") or node.get("lifecycle") or "—",
            _percent(node.get("short_availability")), _percent(node.get("long_availability")),
            "—" if node.get("latency") is None else "%d ms" % round(node["latency"]),
            "—" if node.get("jitter") is None else "%d ms" % round(node["jitter"]),
            node.get("quality", "—"), node.get("samples", "—"), node.get("success_streak", 0), node.get("failure_streak", 0),
            _clock(node["quarantine_until"]) if node.get("quarantined") else "—"))
    if not nodes:
        out.append("没有节点数据")

    out += ["", "[警告与错误]（router-error.log 最后 %d 行）" % MAX_ERROR_LINES]
    out += tail(os.path.join(log_dir, "router-error.log"), MAX_ERROR_LINES) or ["没有记录"]

    text = scrub("\n".join(out) + "\n", home)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        text = encoded[:MAX_BYTES].decode("utf-8", "ignore") + "\n…（已截断）\n"
    return text
