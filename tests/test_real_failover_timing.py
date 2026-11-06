"""Isolated Mihomo failover drill with local, fault-injectable HTTP outbounds."""
import concurrent.futures
import http.server
import json
import os
import pathlib
import socket
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from unittest import mock
from urllib.parse import quote

import cycle_harness
import real_core

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "sim" / "detection"))
from measure_recovery import measure_recovery, proxy_http_probe


class Endpoint(http.server.BaseHTTPRequestHandler):
    def do_CONNECT(self):
        if self.server.mode == "down":
            self.send_error(503)
            return
        self.send_response(200)
        self.end_headers()
        self.handle_one_request()

    def do_GET(self):
        mode = self.server.mode
        if mode == "down":
            self.close_connection = True
            return
        if mode == "stall":
            time.sleep(0.6)
        self.send_response(200)
        self.end_headers()
        try:
            if self.command == "HEAD":
                return
            self.wfile.write(b"steadyroute-drill")
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


@unittest.skipUnless(real_core.available(), "requires an isolated Mihomo binary")
class RealFailoverTimingTests(unittest.TestCase):
    def test_stall_then_two_failovers(self):
        router = cycle_harness.load_router("real_failover_timing_router")
        policy = router.POLICIES[0]
        group = policy["group_name"]
        nodes = list(policy["static_candidates"][:3])
        path = "/proxies/" + quote(group, safe="")
        with ExitStack() as stack:
            core = real_core.Core({"cn": ["example.cn"], "openai": ["example.invalid"],
                                   "category-ai-!cn": ["example.invalid"]})
            stack.callback(core.stop)
            servers = []
            for _ in nodes:
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
                server.mode = "ok"
                threading.Thread(target=server.serve_forever, daemon=True).start()
                stack.callback(server.server_close)
                stack.callback(server.shutdown)
                servers.append(server)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                mixed_port = reservation.getsockname()[1]
            config = dict(mode="rule", **{"mixed-port": mixed_port, "allow-lan": False,
                          "bind-address": "127.0.0.1", "geo-auto-update": False,
                          "proxies": [{"name": n, "type": "http", "server": "127.0.0.1", "port": s.server_port}
                                      for n, s in zip(nodes, servers)],
                          "proxy-groups": [{"name": group, "type": "select", "proxies": nodes}],
                          "rules": ["MATCH," + group]})
            status, body = core.load(json.dumps(config, ensure_ascii=False))
            self.assertEqual(status, 204, body)
            self.assertEqual(core("PUT", path, {"name": nodes[0]})[0], 204)
            url = "http://127.0.0.1:%d/check" % servers[-1].server_port
            client = proxy_http_probe(mixed_port, url, b"steadyroute-drill", timeout=0.3)
            self.assertTrue(client(), core.output()[-2400:])
            state = router.state_contract.new_state()
            now = time.time()
            state["nodes"].update({node: cycle_harness.mature_node(50 + 30*i, now)
                                   for i, node in enumerate(nodes)})
            state["groups"][group] = {"last_seen": nodes[0], "hot_standby": nodes[1]}
            router.RUNTIME["last_state"] = state
            router.reset_detection_memory()
            for name, value in {
                "POLICIES": [policy], "POLICY_BY_GROUP": {group: policy}, "GROUPS": {group: nodes},
                "FAST_PROBE_URL": url, "LOCAL_CHECK_URLS": (url,), "BUSINESS_TEST_URLS": {group: [url]},
                "CONFIRM_STAGGER_SECONDS": 0.15,
            }.items():
                stack.enter_context(mock.patch.object(router, name, value))
            stack.enter_context(mock.patch.dict(os.environ, {"STEADYROUTE_CONTROLLER_SOCKET": core.socket}))
            stack.enter_context(mock.patch.object(router, "load_state", return_value=state))
            stack.enter_context(mock.patch.object(router, "save_state"))
            stack.enter_context(mock.patch.object(router, "update_dashboard_cache"))
            events = stack.enter_context(mock.patch.object(router.logging_setup, "write_event"))
            servers[0].mode = "stall"
            started = time.monotonic()
            self.assertEqual(router.run_fast_tick(), "transient")
            self.assertEqual(core.get(path)["now"], nodes[0])
            print("DRILL stall retained elapsed_ms=%d" % ((time.monotonic() - started) * 1000), flush=True)
            servers[0].mode = "ok"
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                for index in range(2):
                    events.reset_mock()
                    servers[index].mode = "down"
                    fault_at = time.monotonic()
                    recovery = pool.submit(measure_recovery, client, fault_at, interval=0.05)
                    observed = pool.submit(self.observe, core, path, nodes[index + 1], fault_at)
                    self.assertEqual(router.run_fast_tick(), "failover")
                    time.sleep(1)
                    router.fast_plan()
                    result = recovery.result()
                    oracle_ms = observed.result()
                    self.assertTrue(result["outage_observed"], result)
                    self.assertIsNotNone(result["first_success_ms"], result)
                    self.assertIsNotNone(oracle_ms)
                    switch, = [call.kwargs for call in events.call_args_list if call.args == ("failover",)]
                    readback, = [call.kwargs for call in events.call_args_list if call.args == ("failover_readback",)]
                    self.assertEqual(switch["incident_id"], readback["incident_id"])
                    self.assertEqual(readback["outcome"], "observed")
                    print("DRILL " + json.dumps(dict(scenario=index+1, oracle_ms=oracle_ms,
                          recovery=result, timing=switch["timing_ms"], readback=readback["ack_to_observed_ms"])),
                          flush=True)

    @staticmethod
    def observe(core, path, target, fault_at):
        while time.monotonic() - fault_at < 10:
            if core.get(path)["now"] == target:
                return round((time.monotonic() - fault_at) * 1000)
            time.sleep(0.02)
        return None
