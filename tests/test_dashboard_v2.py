"""v0.4.5 dashboard data: standby line, connection summary, same-scale memory peak."""

import pathlib
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402
import runtime_metrics  # noqa: E402

router = cycle_harness.load_router("dashboard_v2_router")
TW = router.POLICIES[0]
TW_GROUP, TW_NODES = TW["group_name"], list(TW["static_candidates"])
MODULE_DIR = cycle_harness.MODULE_DIR


def connection(node, host, start="2026-09-26T08:00:00.123456789Z", up=10, down=100, process="Claude", group=TW_GROUP):
    return {"id": host + node, "chains": [node, group], "upload": up, "download": down, "start": start,
            "metadata": {"host": host, "process": process, "destinationIP": "203.0.113.9"}}


class ConnectionSummaryTests(unittest.TestCase):
    NOW = 1790409600   # 2026-09-26T08:00:00Z

    def test_groups_by_site_and_counts_nodes(self):
        conns = [
            connection(TW_NODES[0], "claude.ai", down=500),
            connection(TW_NODES[0], "claude.ai", down=300),
            connection(TW_NODES[1], "claude.ai", down=50),
            connection(TW_NODES[1], "api.anthropic.com"),
            connection("HK-node", "grok.com", group="other-group"),
        ]
        summary = router.connection_summary(TW_GROUP, conns, self.NOW + 60)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["by_node"], {TW_NODES[0]: 2, TW_NODES[1]: 2})
        first = summary["sites"][0]
        self.assertEqual((first["host"], first["count"], first["download"]), ("claude.ai", 3, 850))
        self.assertEqual(first["nodes"], {TW_NODES[0]: 2, TW_NODES[1]: 1})
        self.assertEqual(first["since"], self.NOW)
        self.assertEqual(first["process"], "Claude")

    def test_never_exposes_ip_addresses(self):
        conns = [connection(TW_NODES[0], "")] + [connection(TW_NODES[0], "198.51.100.4")]
        summary = router.connection_summary(TW_GROUP, conns, self.NOW)
        hosts = [site["host"] for site in summary["sites"]]
        self.assertEqual(hosts, ["IP 直连（地址已隐藏）"])
        self.assertNotIn("203.0.113.9", repr(summary))
        self.assertNotIn("198.51.100.4", repr(summary))

    def test_site_list_is_bounded(self):
        conns = [connection(TW_NODES[0], "site%d.example" % index) for index in range(80)]
        summary = router.connection_summary(TW_GROUP, conns, self.NOW)
        self.assertEqual(len(summary["sites"]), router.CONNECTION_SITE_LIMIT)
        self.assertEqual(summary["more_sites"], 80 - router.CONNECTION_SITE_LIMIT)

    def test_rfc3339_with_offset_and_nanoseconds(self):
        self.assertEqual(router._connection_started_at("2026-09-26T16:00:00.999999999+08:00"), self.NOW)
        self.assertEqual(router._connection_started_at("2026-09-26T08:00:00Z"), self.NOW)
        self.assertIsNone(router._connection_started_at("garbage"))
        self.assertIsNone(router._connection_started_at(None))

    def test_missing_metadata_is_tolerated(self):
        summary = router.connection_summary(TW_GROUP, [{"id": "x", "chains": [TW_NODES[0], TW_GROUP]}], self.NOW)
        self.assertEqual(summary["total"], 1)


class StandbyLineTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.clear()
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        router.TIMELINE.clear()
        self.now = time.time()
        self.net = cycle_harness.FakeNetwork(router)
        self.state = router.state_contract.new_state()
        for policy in router.POLICIES:
            for index, name in enumerate(policy["static_candidates"]):
                self.state["nodes"][name] = cycle_harness.mature_node(80 + 10 * index, self.now)
                self.net.latency[name] = 80 + 10 * index
            self.state["groups"][policy["group_name"]] = {"last_business_probe_at": int(self.now)}

    def test_cycle_records_standby_point_for_the_chart(self):
        self.net.run_cycle(self.state)
        standby = self.state["groups"][TW_GROUP]["hot_standby"]
        points = [item for item in router.TIMELINE[TW_GROUP] if item["kind"] == "standby"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["node"], standby)
        self.assertIn((standby, router.FAST_PROBE_URL), self.net.probe_calls)

    def test_standby_display_probe_never_touches_statistics(self):
        self.net.run_cycle(self.state)
        standby = self.state["groups"][TW_GROUP]["hot_standby"]
        samples = self.state["nodes"][standby]["samples"]
        original = self.net.probe

        def probe(name, url, timeout_ms):
            if name == standby and url == router.FAST_PROBE_URL:
                return None     # display probe fails; base probe still succeeds
            return original(name, url, timeout_ms)

        self.net.probe = probe
        self.net.run_cycle(self.state)
        node = self.state["nodes"][standby]
        self.assertEqual(node["samples"], samples + 1)
        self.assertTrue(node["last_success"])
        self.assertEqual(node.get("recent_failures", []), [])

    def test_legacy_group_has_standby_connections_and_30_minute_timeline(self):
        self.net.connections = [connection(TW_NODES[0], "claude.ai")]
        self.net.run_cycle(self.state)
        for index in range(400):
            router.timeline_add(TW_GROUP, {"t": self.now - 1700 + index * 4, "ms": 50, "node": TW_NODES[0], "kind": "probe"})
        snapshots = router.build_status_snapshots(self.state, self.net.proxy_data, self.net.connections, now=int(self.now))
        legacy = next(g for g in snapshots["legacy"]["groups"] if g["name"] == TW_GROUP)
        self.assertEqual(legacy["hot_standby"], self.state["groups"][TW_GROUP]["hot_standby"])
        self.assertEqual(legacy["connections"]["total"], 1)
        self.assertLess(min(point[0] for point in legacy["timeline"]), self.now - 1500)
        v1 = next(g for g in snapshots["v1"]["groups"] if g["name"] == TW_GROUP)
        self.assertGreaterEqual(min(point[0] for point in v1["recent_probes"]), self.now - 330)
        self.assertIn("version", snapshots["legacy"]["service"])


class SwitchReasonTests(StandbyLineTests):
    def test_failover_is_marked_as_failover(self):
        self.net.run_cycle(self.state)
        self.net.down_nodes.add(TW_NODES[0])
        self.net.run_fast_tick(self.state)
        points = router.public_timeline(TW_GROUP, time.time())
        switches = [point for point in points if point[2] == "switch"]
        self.assertEqual(len(switches), 1)
        self.assertEqual(switches[0][4], "failover")
        self.assertIn(switches[0][3], TW_NODES)

    def test_optimize_is_marked_as_optimize(self):
        # Make the current node much slower than a mature candidate, then let it confirm.
        current = TW_NODES[0]
        self.state["nodes"][current]["latency_ewma"] = 600.0
        self.state["nodes"][current]["score"] = 603.0
        self.net.latency[current] = 600
        for _ in range(router.PERFORMANCE_CONFIRMATIONS + 1):
            self.net.run_cycle(self.state)
        switches = [point for point in router.public_timeline(TW_GROUP, time.time()) if point[2] == "switch"]
        self.assertTrue(switches)
        self.assertEqual(switches[-1][4], "optimize")

    def test_probe_points_have_no_reason(self):
        self.net.run_cycle(self.state)
        probes = [point for point in router.public_timeline(TW_GROUP, time.time()) if point[2] == "probe"]
        self.assertTrue(probes)
        self.assertIsNone(probes[0][4])


class MemoryPeakTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.clear()

    def test_peak_uses_same_scale_as_current(self):
        with mock.patch.object(router.runtime_metrics, "current_footprint_mb", return_value=27.9), \
                mock.patch.object(router.runtime_metrics, "peak_footprint_mb", return_value=29.4):
            router.footprint_now()
        state = router.state_contract.new_state()
        service = router.build_status_snapshots(state, {}, [], now=1000, memory_mb=34.9)["legacy"]["service"]
        self.assertEqual((service["memory_current_mb"], service["memory_peak_mb"], service["memory_mb"]), (27.9, 29.4, 34.9))

    def test_peak_falls_back_to_highest_seen_value(self):
        with mock.patch.object(router.runtime_metrics, "peak_footprint_mb", return_value=None):
            for value in (25.0, 31.0, 28.0):
                with mock.patch.object(router.runtime_metrics, "current_footprint_mb", return_value=value):
                    router.footprint_now()
        self.assertEqual(router.RUNTIME["memory_footprint_peak_mb"], 31.0)

    def test_peak_is_never_below_current(self):
        with mock.patch.object(router.runtime_metrics, "current_footprint_mb", return_value=30.0), \
                mock.patch.object(router.runtime_metrics, "peak_footprint_mb", return_value=12.0):
            router.footprint_now()
        self.assertEqual(router.RUNTIME["memory_footprint_peak_mb"], 30.0)

    def test_os_peak_is_readable_here(self):
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            self.assertGreaterEqual(runtime_metrics.peak_footprint_mb(), runtime_metrics.current_footprint_mb() - 0.5)


class DashboardPageTests(unittest.TestCase):
    def setUp(self):
        self.source = (MODULE_DIR / "dashboard.html").read_text(encoding="utf-8")

    def test_page_loads_nothing_from_the_network(self):
        # The page must work when the proxy is down: no external fonts, scripts or styles.
        self.assertIsNone(re.search(r'(src|href)="https?://', self.source))
        self.assertNotIn("@import", self.source)

    def test_every_tip_has_an_explanation(self):
        keys = set(re.findall(r'data-tip="([a-z]+)"', self.source))
        defined = set(re.findall(r"^  ([a-z]+): \(", self.source, re.M))
        self.assertTrue(keys)
        self.assertLessEqual(keys, defined)

    def test_renamed_metrics(self):
        for label in ("近 24 小时可用率", "平均延迟", "综合评分", "活跃连接"):
            self.assertIn(label, self.source)
        self.assertNotIn("加权可用率", self.source)

    def test_page_only_reads_the_cached_status(self):
        fetches = re.findall(r"fetch\('([^']+)'", self.source)
        self.assertEqual(fetches, ["/api/status"])


if __name__ == "__main__":
    unittest.main()
