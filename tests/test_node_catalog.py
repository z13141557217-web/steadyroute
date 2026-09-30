"""v0.4.5 sub-pages: read-only subscription catalogue, /api/nodes and the static pages."""

import json
import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402
import node_catalog  # noqa: E402

router = cycle_harness.load_router("node_catalog_router")
MODULE_DIR = cycle_harness.MODULE_DIR
PROJECT_DIR = cycle_harness.PROJECT_DIR
TW, HK = router.POLICIES[0], router.POLICIES[1]
NOW = 1790409600  # 2026-09-26T08:00:00Z


def node(kind="Hysteria2", delay=None, at="2026-09-26T15:59:30.123456789+08:00", udp=True):
    item = {"type": kind, "udp": udp, "history": []}
    if delay is not None:
        item["history"] = [{"time": "2026-09-26T07:50:00Z", "delay": 999}, {"time": at, "delay": delay}]
    return item


def subscription():
    return {
        TW["group_name"]: {"type": "Selector", "now": TW["static_candidates"][0], "all": list(TW["static_candidates"])},
        "GLOBAL": {"type": "Selector", "all": ["DIRECT"]},
        "DIRECT": {"type": "Direct"},
        "REJECT": {"type": "Reject"},
        TW["static_candidates"][0]: node(delay=80),
        TW["static_candidates"][1]: node(delay=120),
        TW["static_candidates"][2]: node(delay=0),
        HK["static_candidates"][0]: node(delay=40),
        "台湾 01 | IEPL 专线": node("Trojan", delay=60),
        "🇯🇵 日本 东京 02": node("Shadowsocks", delay=150),
        "美国 洛杉矶 | 3x": node("Vmess", delay=190),
        "USB 测试节点": node("Vless"),
        "剩余流量：120 GB": node("Shadowsocks"),
        "套餐到期：2026-12-31": node("Shadowsocks"),
    }


class RegionTests(unittest.TestCase):
    def test_taiwan_name_wins_over_a_cn_flag(self):
        self.assertEqual(node_catalog.region_of("台湾 HiNet 家宽 01 🇨🇳")[0], "TW")

    def test_common_regions(self):
        cases = {"香港 家宽 01": "HK", "🇯🇵 日本 东京 02": "JP", "US West 01": "US", "新加坡 IPLC": "SG",
                 "Korea Seoul": "KR", "英国 London": "GB", "某个奇怪的节点": "OT"}
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertEqual(node_catalog.region_of(name)[0], code)

    def test_two_letter_codes_need_word_boundaries(self):
        self.assertEqual(node_catalog.region_of("USB 测试节点")[0], "OT")
        self.assertEqual(node_catalog.region_of("Plus 节点")[0], "OT")


class CatalogTests(unittest.TestCase):
    def build(self, **kwargs):
        return node_catalog.build_catalog(subscription(), router.POLICIES, NOW, **kwargs)

    def test_lists_only_real_nodes_and_counts_notices(self):
        catalog = self.build()
        names = {item["name"] for item in catalog["nodes"]}
        self.assertNotIn(TW["group_name"], names)
        self.assertNotIn("DIRECT", names)
        self.assertNotIn("剩余流量：120 GB", names)
        self.assertEqual(catalog["notices_hidden"], 2)
        self.assertEqual(catalog["total"], 8)

    def test_roles_follow_steadyroute_not_the_name(self):
        catalog = self.build(current=[TW["static_candidates"][0]], standby=[TW["static_candidates"][1]])
        role = {item["name"]: item["role"] for item in catalog["nodes"]}
        self.assertEqual(role[TW["static_candidates"][0]], "current")
        self.assertEqual(role[TW["static_candidates"][1]], "standby")
        self.assertEqual(role[TW["static_candidates"][2]], "monitored")
        self.assertEqual(role[HK["static_candidates"][0]], "monitored")
        # A Taiwan node that is not residential is listed under Taiwan but never monitored.
        iepl = next(item for item in catalog["nodes"] if item["name"] == "台湾 01 | IEPL 专线")
        self.assertEqual((iepl["region"], iepl["role"], iepl["residential"], iepl["group"]), ("TW", "view", False, None))

    def test_monitored_nodes_carry_their_group_and_region(self):
        catalog = self.build()
        hk = next(item for item in catalog["nodes"] if item["name"] == HK["static_candidates"][0])
        self.assertEqual((hk["region"], hk["group"], hk["residential"]), ("HK", HK["group_name"], True))

    def test_clash_delay_is_the_latest_history_entry(self):
        catalog = self.build()
        by_name = {item["name"]: item for item in catalog["nodes"]}
        first = by_name[TW["static_candidates"][0]]
        self.assertEqual((first["delay_ms"], first["delay_at"]), (80, NOW - 30))
        self.assertEqual(by_name[TW["static_candidates"][2]]["delay_ms"], 0)     # tested and failed
        self.assertIsNone(by_name["USB 测试节点"]["delay_ms"])                   # never tested

    def test_order_regions_then_roles_then_delay(self):
        catalog = self.build(current=[TW["static_candidates"][1]])
        codes = [region["code"] for region in catalog["regions"]]
        self.assertEqual(codes, ["TW", "HK", "JP", "US", "OT"])
        tw = [item["name"] for item in catalog["nodes"] if item["region"] == "TW"]
        self.assertEqual(tw[0], TW["static_candidates"][1])                    # current first
        self.assertEqual(tw[-1], "台湾 01 | IEPL 专线")                           # view-only last
        self.assertLess(tw.index(TW["static_candidates"][0]), tw.index(TW["static_candidates"][2]))  # failed after ok
        self.assertEqual(sum(region["count"] for region in catalog["regions"]), catalog["total"])

    def test_catalog_is_bounded(self):
        data = {"台湾 %04d" % index: node() for index in range(node_catalog.CATALOG_LIMIT + 25)}
        catalog = node_catalog.build_catalog(data, router.POLICIES, NOW)
        self.assertEqual(catalog["total"], node_catalog.CATALOG_LIMIT)
        self.assertEqual(catalog["truncated"], 25)

    def test_tolerates_malformed_entries(self):
        data = {"a": None, "b": {"type": None}, 3: {"type": "Trojan"}, "c": {"type": "Trojan", "history": "x"}}
        catalog = node_catalog.build_catalog(data, router.POLICIES, NOW)
        self.assertEqual([item["name"] for item in catalog["nodes"]], ["c"])


class NodesEndpointTests(unittest.TestCase):
    def setUp(self):
        router.NODES_CACHE = None

    def test_unavailable_until_first_cycle(self):
        self.assertEqual(router.cached_api_response("/api/nodes")[0], 503)

    def test_served_from_the_cache(self):
        state = router.state_contract.new_state()
        state["groups"][TW["group_name"]] = {"hot_standby": TW["static_candidates"][1]}
        router.update_nodes_cache(state, subscription(), NOW)
        status, content_type, body = router.cached_api_response("/api/nodes")
        self.assertEqual((status, content_type), (200, "application/json; charset=utf-8"))
        catalog = json.loads(body.decode("utf-8"))
        role = {item["name"]: item["role"] for item in catalog["nodes"]}
        self.assertEqual(role[TW["static_candidates"][0]], "current")
        self.assertEqual(role[TW["static_candidates"][1]], "standby")

    def test_catalog_failure_never_breaks_a_cycle(self):
        state = router.state_contract.new_state()
        with mock.patch.object(router.node_catalog, "build_catalog", side_effect=RuntimeError("boom")), \
                mock.patch.object(router, "save_state"), mock.patch.object(router, "sync_state_events"):
            router.publish_state(state, subscription(), [], NOW)
        self.assertEqual(router.cached_api_response("/api/status")[0], 200)
        self.assertIsNone(router.read_nodes_cache())


class StaticPageTests(unittest.TestCase):
    PAGES = {"/nodes": "text/html", "/settings": "text/html", "/guide": "text/html", "/changelog": "text/html", "/assets/pages.css": "text/css"}

    def test_pages_are_served(self):
        for route, kind in self.PAGES.items():
            with self.subTest(route=route):
                status, content_type, body = router.static_page_response(route)
                self.assertEqual(status, 200)
                self.assertTrue(content_type.startswith(kind))
                self.assertTrue(body)
        self.assertEqual(router.static_page_response("/guide/")[0], 200)
        self.assertEqual(router.static_page_response("/nodes.html")[0], 404)
        self.assertEqual(router.static_page_response("/../state.json")[0], 404)

    def source(self, name):
        return (MODULE_DIR / name).read_text(encoding="utf-8")

    def test_pages_load_nothing_from_the_network(self):
        for name in ("nodes.html", "guide.html", "changelog.html", "pages.css"):
            with self.subTest(page=name):
                text = self.source(name)
                self.assertIsNone(re.search(r'(src|href)="https?://', text))
                self.assertIsNone(re.search(r"url\(\s*['\"]?https?://", text))
                self.assertNotIn("@import", text)

    def test_pages_read_only_cached_endpoints(self):
        allowed = {"/api/status", "/api/nodes"}
        for name in ("nodes.html", "guide.html", "changelog.html"):
            with self.subTest(page=name):
                self.assertLessEqual(set(re.findall(r"fetch\('([^']+)'", self.source(name))), allowed)
                self.assertNotRegex(self.source(name), r"method:\s*'(POST|PUT|DELETE|PATCH)'")

    def test_guide_has_every_anchor_the_dashboard_links_to(self):
        anchors = set(re.findall(r"anchor: '([a-z-]+)'", self.source("dashboard.html")))
        self.assertTrue(anchors)
        guide = self.source("guide.html")
        for anchor in anchors | {"detection", "optimize", "quarantine", "iron-rule"}:
            with self.subTest(anchor=anchor):
                self.assertIn('id="%s"' % anchor, guide)

    def test_changelog_page_covers_every_release(self):
        versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (PROJECT_DIR / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
        page = self.source("changelog.html")
        for version in versions:
            with self.subTest(version=version):
                self.assertIn("'%s'" % version, page)

    def test_sub_pages_share_the_dashboard_design_tokens(self):
        def tokens(text):
            return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", text[text.index(":root {"):text.index("/* ---")]))
        self.assertEqual(tokens(self.source("pages.css")), tokens(self.source("dashboard.html")))

    def test_every_page_links_to_every_other_page(self):
        for name in ("dashboard.html", "nodes.html", "guide.html", "changelog.html", "settings.html"):
            with self.subTest(page=name):
                text = self.source(name)
                for href in ('href="/"', 'href="/nodes"', 'href="/guide"', 'href="/changelog"', 'href="/settings"'):
                    self.assertIn(href, text)


if __name__ == "__main__":
    unittest.main()
