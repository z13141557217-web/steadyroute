"""Scripted in-process network for run_cycle tests. No sockets, no real time."""

import importlib.util
import pathlib
import sys
import threading
import time
from unittest import mock


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))


# The tests exercise the fixed Taiwan / Hong Kong setup (anonymised node names) unless a test
# points STEADYROUTE_POLICY_CONFIG elsewhere before loading the router.
FIXED_CONFIG = PROJECT_DIR / "tests" / "fixtures" / "route-policies.fixed.json"


def load_router(name):
    import os
    os.environ.setdefault("STEADYROUTE_POLICY_CONFIG", str(FIXED_CONFIG))
    try:
        return _load_router(name)
    finally:
        if os.environ.get("STEADYROUTE_POLICY_CONFIG") == str(FIXED_CONFIG):
            del os.environ["STEADYROUTE_POLICY_CONFIG"]


def _load_router(name):
    spec = importlib.util.spec_from_file_location(name, str(MODULE_DIR / "weighted_router.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Real pacing between confirmation probes (0.7 s) only slows tests down; no test measures it.
    module.CONFIRM_STAGGER_SECONDS = 0
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
        self.realtime = False            # probes take real time (timeouts, stalls, latency)
        self.stall = {}                  # node -> time.monotonic() until which it does not answer
        self.slow_urls = {}              # url -> extra seconds (real time only)
        self.connection_growth = {}      # connection id -> bytes added on every /connections read
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
            outcome = self._outcome(name, url)
        if not self.realtime:
            return outcome
        # Real time: a probe that gets no answer takes its whole timeout; a node in a stall
        # answers once the stall ends, if that is within the timeout.
        if name == "DIRECT" and outcome is not None:
            return outcome
        now = time.monotonic()
        stall_end = self.stall.get(name, 0)
        if outcome is not None and now < stall_end:
            wait = stall_end - now + outcome / 1000.0
            if wait * 1000 < timeout_ms:
                time.sleep(wait)
                return int(wait * 1000)
            time.sleep(timeout_ms / 1000.0)
            return None
        if outcome is None:
            time.sleep(timeout_ms / 1000.0)
            return None
        delay = self.slow_urls.get(url, 0) + outcome / 1000.0
        time.sleep(min(delay, timeout_ms / 1000.0))
        return int(delay * 1000) if delay * 1000 < timeout_ms else None

    def _outcome(self, name, url):
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
        if method == "GET" and path.startswith("/proxies/") and "/delay" not in path:
            from urllib.parse import unquote
            item = self.proxy_data.get(unquote(path[len("/proxies/"):]))
            if item is None:
                raise RuntimeError("404 %s" % path)
            return item
        if method == "GET" and path == "/connections":
            with self.lock:
                for item in self.connections:
                    item["download"] = int(item.get("download", 0)) + int(self.connection_growth.get(item.get("id"), 0))
                return {"connections": [dict(item) for item in self.connections]}
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

    def run_fast_tick(self, state):
        with mock.patch.object(self.router, "load_state", return_value=state), \
                mock.patch.object(self.router, "save_state"), \
                mock.patch.object(self.router, "update_dashboard_cache"), \
                mock.patch.object(self.router, "log"), \
                mock.patch.object(self.router, "api_request", side_effect=self.api), \
                mock.patch.object(self.router, "probe_url", side_effect=self.probe):
            return self.router.run_fast_tick(dry_run=False)

    def calls_to(self, name):
        return [call for call in self.probe_calls if call[0] == name]
