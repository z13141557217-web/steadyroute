"""Scripted in-process network for run_cycle tests. No sockets, no real time."""

import importlib.util
import pathlib
import sys
import threading
from unittest import mock


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))


def load_router(name):
    spec = importlib.util.spec_from_file_location(name, str(MODULE_DIR / "weighted_router.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mature_node(latency, now, business_ok=True):
    hour = int(now // 3600)
    return {
        "last_success": True,
        "samples": 20,
        "success_streak": 20,
        "failure_streak": 0,
        "availability_ewma": 1.0,
        "latency_ewma": float(latency),
        "jitter_ewma": 2.0,
        "score": float(latency) + 3.0,
        "short_results": [1] * 20,
        "long_buckets": [{"hour": hour, "success": 100, "total": 100, "latency_sum": int(latency) * 100}],
        "last_probe_at": int(now) - 20,
        "business_successes": 5 if business_ok else 0,
        "business_last_success": business_ok,
        "business_checked_at": int(now) - 30,
    }


class FakeNetwork(object):
    """Answers probe_url and the controller API from simple, mutable rules."""

    def __init__(self, router):
        self.router = router
        self.lock = threading.Lock()
        self.down_nodes = set()          # base and business probes fail
        self.node_url_failures = set()   # (node, url) pairs that fail
        self.site_down = set()           # urls that fail through every node
        self.local_offline = False       # everything fails, including DIRECT
        self.fail_once = {}              # node -> remaining failures before recovering
        self.latency = {}                # node -> ms
        self.probe_calls = []
        self.puts = []
        self.deletes = []
        self.proxy_data = {}
        self.connections = []
        for policy in router.POLICIES:
            names = list(policy["static_candidates"])
            self.proxy_data[policy["group_name"]] = {"now": names[0], "all": names}
            self.proxy_data[policy["discovery_group_name"]] = {"now": names[0], "all": names}

    def current(self, group_name):
        return self.proxy_data[group_name]["now"]

    def probe(self, name, url, timeout_ms):
        with self.lock:
            self.probe_calls.append((name, url))
            if self.local_offline:
                return None
            if name == "DIRECT":
                return 20
            if self.fail_once.get(name, 0) > 0:
                self.fail_once[name] -= 1
                return None
            if name in self.down_nodes or url in self.site_down or (name, url) in self.node_url_failures:
                return None
            return int(self.latency.get(name, 80))

    def api(self, method, path, payload=None, timeout=10):
        if method == "GET" and path == "/proxies":
            return {"proxies": self.proxy_data}
        if method == "GET" and path == "/connections":
            return {"connections": list(self.connections)}
        if method == "PUT" and path.startswith("/proxies/"):
            from urllib.parse import unquote
            group = unquote(path[len("/proxies/"):])
            self.puts.append((group, payload["name"]))
            self.proxy_data[group]["now"] = payload["name"]
            return None
        if method == "DELETE" and path.startswith("/connections/"):
            self.deletes.append(path)
            return None
        raise AssertionError((method, path))

    def run_cycle(self, state):
        with mock.patch.object(self.router, "load_state", return_value=state), \
                mock.patch.object(self.router, "save_state"), \
                mock.patch.object(self.router, "update_dashboard_cache"), \
                mock.patch.object(self.router, "log"), \
                mock.patch.object(self.router, "api_request", side_effect=self.api), \
                mock.patch.object(self.router, "probe_url", side_effect=self.probe):
            self.router.run_cycle(dry_run=False)

    def calls_to(self, name):
        return [call for call in self.probe_calls if call[0] == name]
