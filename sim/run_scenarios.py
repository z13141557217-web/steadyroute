#!/usr/bin/env python3
"""Run acceptance scenarios against two SteadyRoute versions and record results.

Usage: run_scenarios.py --old OLD_SOURCE --new NEW_SOURCE --out results.json [--only NAME]

Every run gets its own fake controller, state directory and dashboard port, so
the old and new versions face identical, independent networks in parallel.
"""

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fake_mihomo  # noqa: E402

PROJECT = pathlib.Path(__file__).resolve().parents[1]
POLICY = json.loads((PROJECT / "tests" / "fixtures" / "route-policies.fixed.json").read_text(encoding="utf-8"))
TW = POLICY["policies"][0]
HK = POLICY["policies"][1]
TW_GROUP, HK_GROUP = TW["group_name"], HK["group_name"]

# Latency / jitter modelled on the production dashboard screenshots (ms).
PROFILE = {
    "台湾 HiNet 家宽 01 🇨🇳": (57, 3),
    "台湾 HiNet 家宽 02 🇨🇳": (80, 4),
    "台湾 HiNet 家宽 03 🇨🇳": (96, 6),
    "台湾 HiNet 家宽 04": (122, 11),
    "台湾 HiNet 家宽 05": (159, 40),
    "台湾 HiNet 家宽 06": (140, 12),
    "台湾 HiNet 家宽 07": (215, 92),
    "台湾 HiNet 家宽 08": (143, 15),
    "台湾 HiNet 家宽 09": (170, 20),
    "台湾 HiNet 家宽 10": (180, 25),
    "台湾 HiNet 家宽 11": (110, 9),
    "香港 家宽 01": (36, 4),
    "香港 家宽 02": (90, 8),
    "香港 家宽 03": (321, 200),
    "香港 家宽 04": (232, 120),
    "香港 家宽 05": (150, 15),
}
DEAD = {"台湾 HiNet 家宽 07", "香港 家宽 03"}
FLAKY = {"台湾 HiNet 家宽 04": 0.35, "香港 家宽 04": 0.2}
TW_CURRENT = "台湾 HiNet 家宽 02 🇨🇳"
HK_CURRENT = "香港 家宽 01"
CLAUDE_URL = next(url for url in TW["business_test_urls"] if "claude" in url)

SCENARIOS = [
    {
        "id": "normal",
        "title": "正常运行",
        "question": "看板“最近检测”能否实时？一轮检测要多久？",
        "duration": 200,
        "timeline": [],
    },
    {
        "id": "node_down",
        "title": "当前台湾节点突然断开",
        "question": "从断开到切到健康节点要多久？AI 连接何时被重建？",
        "duration": 210,
        "timeline": [{"t": 60, "action": "node_down", "node": TW_CURRENT, "label": "当前台湾节点断开"}],
    },
    {
        "id": "site_down",
        "title": "claude.ai 自身故障（节点正常）",
        "question": "会不会把网站故障误判成节点故障而乱切？",
        "duration": 260,
        "timeline": [
            {"t": 30, "action": "site_down", "url": CLAUDE_URL, "label": "claude.ai 不可达"},
            {"t": 230, "action": "site_up", "url": CLAUDE_URL, "label": "claude.ai 恢复"},
        ],
    },
    {
        "id": "local_offline",
        "title": "本机断网 45 秒",
        "question": "断网期间会不会误切、误隔离节点？",
        "duration": 210,
        "timeline": [
            {"t": 60, "action": "local_offline", "label": "本机断网"},
            {"t": 105, "action": "local_online", "label": "网络恢复"},
        ],
    },
    {
        "id": "sleep",
        "title": "合盖休眠 150 秒后唤醒",
        "question": "唤醒瞬间连接重建期间会不会误判故障？",
        "duration": 320,
        "timeline": [
            {"t": 60, "action": "sleep", "label": "合盖休眠"},
            {"t": 210, "action": "wake", "blackout": 8, "label": "唤醒（前 8 秒所有连接重建中）"},
        ],
    },
    {
        "id": "slow",
        "title": "当前台湾节点变慢（未断线）",
        "question": "会不会无损回优到更快的节点？旧连接是否保留在原节点？",
        "duration": 220,
        "timeline": [{"t": 40, "action": "latency", "node": TW_CURRENT, "ms": 430, "label": "当前台湾节点延迟升到 430 ms"}],
    },
    {
        "id": "soak",
        "title": "长时间平稳运行（内存与 CPU）",
        "question": "常驻内存和 CPU 占用是多少？快速通道有没有额外开销？",
        "duration": 600,
        "timeline": [],
        "optional": True,
    },
]


def process_usage(pid):
    """(rss_mb, cpu_seconds) of a child process from /proc; (None, None) elsewhere."""
    try:
        with open("/proc/%d/status" % pid, "r", encoding="ascii") as handle:
            rss = next(float(line.split()[1]) / 1024.0 for line in handle if line.startswith("VmRSS:"))
        with open("/proc/%d/stat" % pid, "r", encoding="ascii") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return round(rss, 1), round((int(fields[11]) + int(fields[12])) / float(ticks), 2)
    except (OSError, StopIteration, ValueError, IndexError):
        return None, None


# (group, host, app, bytes per second, lifetime seconds or None for long-lived streams)
CLIENT_MIX = [
    (TW_GROUP, "claude.ai", "Claude", 1500, None), (TW_GROUP, "claude.ai", "Claude", 600, None),
    (TW_GROUP, "claude.ai", "Google Chrome", 300, None), (TW_GROUP, "api.anthropic.com", "Claude", 2400, None),
    (TW_GROUP, "statsig.anthropic.com", "Claude", 40, 25), (TW_GROUP, "chatgpt.com", "Google Chrome", 900, None),
    (TW_GROUP, "ab.chatgpt.com", "Google Chrome", 20, 35), (TW_GROUP, "gemini.google.com", "Safari", 500, None),
    (HK_GROUP, "grok.com", "Google Chrome", 700, None), (HK_GROUP, "x.ai", "Google Chrome", 60, 30),
    (HK_GROUP, "github.com", "Git", 1200, None),
]
SHORT_LIVED = {host: lifetime for _group, host, _process, _rate, lifetime in CLIENT_MIX if lifetime}


def seed_state(now):
    """A state file shaped like a long-running production instance."""
    hour = int(now // 3600)
    nodes = {}
    for name, (latency, jitter) in PROFILE.items():
        dead = name in DEAD
        flaky = FLAKY.get(name, 0.0)
        availability = 0.62 if dead else 1.0 - flaky
        buckets = []
        for age in range(24):
            total = 180 if name in (TW_CURRENT, HK_CURRENT) else 16
            success = int(round(total * availability))
            buckets.append({"hour": hour - 23 + age, "success": success, "total": total,
                            "latency_sum": success * latency})
        node = {
            "samples": 400,
            "last_success": not dead,
            "success_streak": 0 if dead else 30,
            "failure_streak": 3 if dead else 0,
            "effective_failure_streak": 0,
            "availability_ewma": 0.3 if dead else availability,
            "latency_ewma": float(latency),
            "jitter_ewma": float(jitter),
            "score": float(latency) + 1.5 * jitter + 4000 * (1 - (0.3 if dead else availability)),
            "short_results": ([0] * 8 + [1] * 12) if dead else [1] * 20,
            "long_buckets": buckets,
            "recent_failures": [],
            "last_delay": None if dead else latency,
            "last_probe_at": int(now) - 25,
            "last_success_at": int(now) - 25,
            "business_successes": 20,
            "business_last_success": not dead,
            "business_checked_at": int(now) - 40,
        }
        if dead:
            node["quarantine_until"] = int(now) + 1500
        nodes[name] = node
    groups = {
        TW_GROUP: {"last_seen": TW_CURRENT, "last_router_selection": TW_CURRENT, "last_switch_at": int(now) - 7200,
                   "last_business_probe_at": int(now) - 30},
        HK_GROUP: {"last_seen": HK_CURRENT, "last_router_selection": HK_CURRENT, "last_switch_at": int(now) - 7200,
                   "last_business_probe_at": int(now) - 50},
    }
    return {
        "schema_version": 2, "nodes": nodes, "groups": groups, "events": [],
        "subscription": {"generation": 0, "candidate_count": 0, "added_count": 0, "removed_count": 0, "changes": []},
        "updated_at": int(now) - 15, "controller_connected": True,
    }


def build_world(seed):
    groups = {}
    for policy, current in ((TW, TW_CURRENT), (HK, HK_CURRENT)):
        names = list(policy["static_candidates"])
        groups[policy["group_name"]] = {"now": current, "all": names}
        groups[policy["discovery_group_name"]] = {"now": names[0], "all": names}
    world = fake_mihomo.World(
        groups, {k: v[0] for k, v in PROFILE.items()}, {k: v[1] for k, v in PROFILE.items()}, seed=seed)
    world.down_nodes.update(DEAD)
    world.flaky.update(FLAKY)
    return world


def fetch(port, path, timeout=0.8):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


class Run(object):
    def __init__(self, scenario, label, source, port, workdir):
        self.scenario = scenario
        self.label = label
        self.source = source
        self.port = port
        self.dir = pathlib.Path(workdir) / ("%s-%s" % (scenario["id"], label))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.socket = str(self.dir / "mihomo.sock")
        self.world = build_world(seed=hash(scenario["id"]) & 0xFFFF)
        self.samples = []
        self.snapshots = []
        self.markers = []
        self.process = None
        self.stop_flag = threading.Event()

    def start(self, started):
        self.started = started
        self.world.started = started
        (self.dir / "state.json").write_text(json.dumps(seed_state(started), ensure_ascii=False), encoding="utf-8")
        self.server = fake_mihomo.serve(self.world, self.socket)
        for group, host, process, rate, _lifetime in CLIENT_MIX:
            self.world.open_stream(group, host, process, rate)
        # v0.4.4+ writes its own bounded router.log into this directory; stdout is only a fallback.
        log = open(str(self.dir / "bootstrap.log"), "w")
        self.process = subprocess.Popen(
            [sys.executable, str(PROJECT / "sim" / "run_router.py"), self.source, str(self.dir), self.socket, str(self.port)],
            stdout=log, stderr=subprocess.STDOUT)
        threading.Thread(target=self.record_loop, daemon=True).start()
        threading.Thread(target=self.client_loop, daemon=True).start()

    def rel(self):
        return round(time.time() - self.started, 2)

    def client_loop(self):
        """Apps on the Mac: long-lived streams reconnect when closed; short requests come
        and go, so after a switch new requests land on the new node while old streams stay."""
        while not self.stop_flag.wait(0.5):
            now = time.time()
            with self.world.lock:
                for cid, item in list(self.world.connections.items()):
                    lifetime = SHORT_LIVED.get(item.get("host"))
                    if lifetime and now - item.get("start", now) > lifetime:
                        self.world.connections.pop(cid)
                present = {}
                for item in self.world.connections.values():
                    key = (item["group"], item.get("host"), item.get("process"))
                    present[key] = present.get(key, 0) + 1
            wanted = {}
            for group, host, process, rate, _lifetime in CLIENT_MIX:
                key = (group, host, process)
                wanted[key] = wanted.get(key, 0) + 1
                if present.get(key, 0) < wanted[key]:
                    present[key] = present.get(key, 0) + 1
                    self.world.open_stream(group, host, process, rate)
                    if host == "claude.ai":
                        self.world.record("reconnect", group=group, node=self.world.groups[group]["now"])

    def record_loop(self):
        last_id = None
        while not self.stop_flag.wait(1.0):
            t = self.rel()
            legacy = fetch(self.port, "/api/status")
            row = {"t": t, "tw": self.world.groups[TW_GROUP]["now"], "hk": self.world.groups[HK_GROUP]["now"]}
            if self.process is not None:
                row["rss_mb"], row["cpu_s"] = process_usage(self.process.pid)
            if legacy:
                service = legacy["service"]
                row.update({
                    "age": round(time.time() - service["updated_at"], 1) if service.get("updated_at") else None,
                    "state": service.get("state_code"),
                    "stale_at": service.get("stale_at"),
                    "duration": None,
                    "decisions": {g["name"]: g.get("decision_code") for g in legacy["groups"]},
                })
                if legacy.get("snapshot_id") != last_id:
                    last_id = legacy.get("snapshot_id")
                    self.snapshots.append({"t": t, "wall": time.time(), "data": legacy})
                v1 = fetch(self.port, "/api/v1/status")
                if v1:
                    row["duration"] = v1["service"].get("last_cycle_duration_ms")
            else:
                row["unreachable"] = True
            self.samples.append(row)

    def apply(self, event):
        action = event["action"]
        world = self.world
        with world.lock:
            if action == "node_down":
                world.down_nodes.add(event["node"])
            elif action == "node_up":
                world.down_nodes.discard(event["node"])
            elif action == "site_down":
                world.site_down.add(event["url"])
            elif action == "site_up":
                world.site_down.discard(event["url"])
            elif action == "local_offline":
                world.local_offline = True
            elif action == "local_online":
                world.local_offline = False
            elif action == "wake":
                world.blackout_until = time.time() + float(event.get("blackout", 0))
            elif action == "latency":
                world.latency[event["node"]] = float(event["ms"])
        if action == "sleep":
            self.process.send_signal(signal.SIGSTOP)
        elif action == "wake":
            self.process.send_signal(signal.SIGCONT)
        self.markers.append({"t": self.rel(), "label": event["label"], "action": action})

    def read_logs(self):
        lines = []
        for name in ("bootstrap.log", "router.log"):
            path = self.dir / name
            if path.exists():
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        return lines

    def finish(self):
        self.stop_flag.set()
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGCONT)
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.server.shutdown()
        state = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        return {
            "label": self.label,
            "samples": self.samples,
            "snapshots": self.snapshots,
            "markers": self.markers,
            "controller": [entry for entry in self.world.log if entry["kind"] != "probe"],
            "probes": [entry for entry in self.world.log if entry["kind"] == "probe"],
            "events": state.get("events", []),
            "final_nodes": {
                name: {
                    "quarantined": int(item.get("quarantine_until", 0)) > time.time(),
                    "failure_streak": item.get("failure_streak", 0),
                    "short_failures": item.get("short_results", []).count(0),
                }
                for name, item in state.get("nodes", {}).items()
            },
            "router_log": self.read_logs()[-400:],
            "log_files": {
                item.name: item.stat().st_size for item in sorted(self.dir.iterdir())
                if item.name.startswith(("router", "events", "bootstrap"))
            },
        }


def run_scenario(scenario, sources, ports, workdir, results):
    runs = [Run(scenario, label, sources[label], ports[label], workdir) for label in ("old", "new")]
    started = time.time()
    for run in runs:
        run.start(started)
    for event in sorted(scenario["timeline"], key=lambda item: item["t"]):
        delay = started + event["t"] - time.time()
        if delay > 0:
            time.sleep(delay)
        for run in runs:
            run.apply(event)
    remaining = started + scenario["duration"] - time.time()
    if remaining > 0:
        time.sleep(remaining)
    results[scenario["id"]] = {
        "scenario": {key: scenario[key] for key in ("id", "title", "question", "duration", "timeline")},
        "runs": {run.label: run.finish() for run in runs},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only")
    parser.add_argument("--soak", action="store_true", help="also run the 10-minute memory/CPU soak")
    args = parser.parse_args()
    workdir = tempfile.mkdtemp(prefix="steadyroute-sim-")
    results = {}
    threads = []
    port = 18700
    for scenario in SCENARIOS:
        if args.only and scenario["id"] != args.only:
            continue
        if not args.only and scenario.get("optional") and not args.soak:
            continue
        ports = {"old": port, "new": port + 1}
        port += 2
        thread = threading.Thread(target=run_scenario, args=(
            scenario, {"old": args.old, "new": args.new}, ports, workdir, results))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    payload = {
        "generated_at": time.time(),
        "profile": {name: {"latency": value[0], "jitter": value[1], "dead": name in DEAD,
                           "flaky": FLAKY.get(name, 0.0)} for name, value in PROFILE.items()},
        "groups": {"tw": TW_GROUP, "hk": HK_GROUP},
        "results": results,
    }
    pathlib.Path(args.out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print("wrote", args.out, "workdir", workdir)


if __name__ == "__main__":
    main()
