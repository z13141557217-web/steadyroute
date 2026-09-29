"""v0.5.0 share edition: lock each of the user's own select groups to its current country and
only ever switch among that country's residential nodes (issue #32)."""

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

import auto_lock  # noqa: E402
import regions  # noqa: E402
import route_policy  # noqa: E402

EXAMPLE_CONFIG = cycle_harness.PROJECT_DIR / "config" / "route-policies.auto-lock.json"
os.environ["STEADYROUTE_POLICY_CONFIG"] = str(EXAMPLE_CONFIG)
try:
    router = cycle_harness.load_router("auto_lock_router")
finally:
    del os.environ["STEADYROUTE_POLICY_CONFIG"]

US_RES = ["🇺🇸 美国 家宽 01", "🇺🇸 美国 家宽 02", "US Residential 03"]
US_DC = ["🇺🇸 美国 洛杉矶 01", "🇺🇸 美国 圣何塞 02"]
JP_RES = ["🇯🇵 日本 家宽 01", "JP-Tokyo-ISP 02"]
JP_DC = ["🇯🇵 日本 东京 01"]
KR_DC = ["🇰🇷 韩国 首尔 01"]
HK_DC = ["🇭🇰 香港 01"]
NOTICE = ["剩余流量：100G", "美国 家宽 到期：2026-12-01"]
ALL_NODES = US_RES + US_DC + JP_RES + JP_DC + KR_DC + HK_DC + NOTICE

GROUP = "🚀 节点选择"
AI_GROUP = "🤖 AI"


def friend_proxies(now_main=US_RES[0], now_ai=None, extra=None):
    """A typical airport subscription as Clash Verge serves it."""
    proxies = {name: {"type": "Trojan", "name": name} for name in ALL_NODES}
    proxies["DIRECT"] = {"type": "Direct"}
    proxies["REJECT"] = {"type": "Reject"}
    members = ALL_NODES + ["DIRECT"]
    proxies["♻️ 自动选择"] = {"type": "URLTest", "now": US_DC[0], "all": US_DC + JP_DC}
    proxies[GROUP] = {"type": "Selector", "now": now_main, "all": ["♻️ 自动选择"] + members}
    proxies["🎯 全球直连"] = {"type": "Selector", "now": "DIRECT", "all": ["DIRECT", GROUP]}
    proxies["🐟 漏网之鱼"] = {"type": "Selector", "now": GROUP, "all": [GROUP, "DIRECT"]}
    proxies["GLOBAL"] = {"type": "Selector", "now": US_RES[0], "all": members}
    if now_ai:
        proxies[AI_GROUP] = {"type": "Selector", "now": now_ai, "all": members}
    proxies.update(extra or {})
    return proxies


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.state = {"groups": {}}
        self.now = 1790409600

    def build(self, proxies, settings=None):
        return auto_lock.build_policies(proxies, self.state, self.now, settings)

    def test_only_select_groups_on_a_real_node_are_managed(self):
        proxies = friend_proxies(now_ai=JP_RES[0])
        proxies["hidden"] = {"type": "Selector", "now": US_RES[0], "all": US_RES, "hidden": True}
        self.assertEqual(auto_lock.managed_groups(proxies), [GROUP, AI_GROUP])
        self.assertEqual(auto_lock.managed_groups(proxies, exclude=[AI_GROUP]), [GROUP])

    def test_locks_to_current_country_with_residential_candidates_only(self):
        policies, statuses, events = self.build(friend_proxies())
        self.assertEqual(len(policies), 1)
        policy = policies[0]
        self.assertEqual((policy["group_name"], policy["region"], policy["region_label"]), (GROUP, "US", "美国"))
        self.assertEqual(policy["static_candidates"], US_RES)
        self.assertEqual(statuses[GROUP]["status"], "locked")
        self.assertEqual(statuses[GROUP]["candidates"], 3)
        self.assertEqual(statuses[GROUP]["same_country_nodes"], 6, "3 residential + 2 datacenter + notice-like 到期 node")
        self.assertEqual(events, [{"kind": "locked", "group": GROUP, "country": "US", "previous": None, "candidates": 3}])
        self.assertEqual(self.state["auto_lock"][GROUP]["country"], "US")
        # Stable afterwards: no new events.
        self.assertEqual(self.build(friend_proxies())[2], [])

    def test_candidates_never_include_other_countries_notices_or_builtins(self):
        policy = self.build(friend_proxies())[0][0]
        for name in JP_RES + JP_DC + KR_DC + HK_DC + NOTICE + US_DC + ["DIRECT", "REJECT"]:
            self.assertFalse(route_policy.name_matches(policy, name), name)

    def test_user_switching_country_relocks(self):
        self.build(friend_proxies())
        policies, statuses, events = self.build(friend_proxies(now_main=JP_DC[0]))
        self.assertEqual(policies[0]["region"], "JP")
        self.assertEqual(policies[0]["static_candidates"], JP_RES)
        self.assertEqual(statuses[GROUP]["status"], "relocked")
        self.assertEqual((statuses[GROUP]["previous"], statuses[GROUP]["previous_label"]), ("US", "美国"))
        self.assertEqual(events[0]["kind"], "relocked")
        self.assertNotEqual(policies[0]["id"], auto_lock._policy_id(GROUP, "US"))

    def test_same_country_change_keeps_lock(self):
        self.build(friend_proxies())
        policies, statuses, events = self.build(friend_proxies(now_main=US_DC[1]))
        self.assertEqual((policies[0]["region"], statuses[GROUP]["status"], events), ("US", "locked", []))
        self.assertFalse(statuses[GROUP]["current_is_residential"])

    def test_country_without_residential_is_monitor_only(self):
        policies, statuses, _events = self.build(friend_proxies(now_main=KR_DC[0]))
        self.assertEqual(policies[0]["static_candidates"], [])
        self.assertEqual(statuses[GROUP]["status"], "no_residential")
        self.assertEqual(statuses[GROUP]["country_label"], "韩国")

    def test_unknown_country_is_left_alone(self):
        proxies = friend_proxies(extra={"节点 A": {"type": "Vmess"}})
        proxies[GROUP]["now"] = "节点 A"
        policies, statuses, events = self.build(proxies)
        self.assertEqual((policies, events), ([], []))
        self.assertEqual(statuses[GROUP], {"status": "unknown_country", "current": "节点 A"})
        self.assertNotIn(GROUP, self.state["auto_lock"])
        # Unknown node later: the existing lock stays.
        self.build(friend_proxies())
        policies = self.build(proxies)[0]
        self.assertEqual(policies[0]["region"], "US")

    def test_groups_lock_independently(self):
        policies, statuses, _ = self.build(friend_proxies(now_ai=JP_RES[1]))
        self.assertEqual({p["group_name"]: p["region"] for p in policies}, {GROUP: "US", AI_GROUP: "JP"})
        self.assertEqual(len({p["id"] for p in policies}), 2)

    def test_prune_registry_drops_stale_auto_records(self):
        self.state["candidate_registry"] = {"policies": {
            auto_lock._policy_id(GROUP, "US"): {}, "auto-dead": {}, "ai-taiwan-residential": {}}}
        policies = self.build(friend_proxies())[0]
        auto_lock.prune_registry(self.state, policies)
        self.assertEqual(sorted(self.state["candidate_registry"]["policies"]),
                         sorted([policies[0]["id"], "ai-taiwan-residential"]))

    def test_business_urls_follow_settings(self):
        policy = self.build(friend_proxies(), {"business_test_urls": ["https://claude.ai/cdn-cgi/trace"]})[0][0]
        self.assertEqual(policy["business_test_urls"], ["https://claude.ai/cdn-cgi/trace"])


class ConfigTests(unittest.TestCase):
    def load(self, config):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "c.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return route_policy.load_policy_config(path)

    def test_example_config_loads(self):
        config = route_policy.load_policy_config(EXAMPLE_CONFIG)
        self.assertEqual((config["profile"], config["policies"]), ("auto_lock", []))

    def test_bad_configs_are_rejected(self):
        base = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        bad = [
            dict(base, profile="magic"),
            dict(base, auto_lock={"exclude_groups": "GLOBAL"}),
            dict(base, auto_lock={"business_test_urls": ["http://claude.ai"]}),
            dict(base, policies=[{"match": "pattern", "region": "US"}]),
            dict(base, policies=[{"match": "country_residential", "region": "OT", "static_candidates": []}]),
            dict(base, policies=[{"match": "country_residential", "region": "US",
                                  "static_candidates": [US_RES[0], JP_RES[0]]}]),
        ]
        for config in bad:
            with self.subTest(config=config):
                with self.assertRaises(route_policy.PolicyConfigError):
                    self.load(config)


class RouterTests(unittest.TestCase):
    """Full cycles against a friend's subscription served by the in-process fake controller."""

    def setUp(self):
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        router.apply_policies([])
        router.AUTO_LOCK_STATUS.clear()
        self.now = time.time()
        self.net = cycle_harness.FakeNetwork(router)
        self.state = router.state_contract.new_state()
        for index, name in enumerate(US_RES + US_DC + JP_RES + JP_DC + KR_DC + HK_DC):
            latency = 80 + 10 * index
            self.state["nodes"][name] = cycle_harness.mature_node(latency, self.now)
            self.net.latency[name] = latency

    def serve(self, proxies):
        self.net.proxy_data = proxies

    def cycle(self):
        self.net.run_cycle(self.state)

    def assert_puts_within(self, allowed):
        for group, name in self.net.puts:
            self.assertIn(name, allowed, "%s was switched to %s" % (group, name))

    def test_first_cycle_locks_and_routes_the_friends_group(self):
        self.serve(friend_proxies())
        self.cycle()
        self.assertEqual(list(router.GROUPS), [GROUP])
        self.assertEqual(router.GROUPS[GROUP], US_RES)
        self.assertEqual(router.POLICY_BY_GROUP[GROUP]["region"], "US")
        self.assertEqual(self.net.puts, [], "a healthy residential node is left as is")
        self.assertEqual(router.AUTO_LOCK_STATUS[GROUP]["status"], "locked")

    def test_failover_stays_inside_the_country(self):
        self.serve(friend_proxies())
        self.cycle()
        self.net.down_nodes.add(US_RES[0])
        self.cycle()
        self.assertEqual(self.net.puts, [(GROUP, US_RES[1])])

    def test_every_residential_node_down_never_crosses_the_border(self):
        self.serve(friend_proxies())
        self.cycle()
        self.net.down_nodes.update(US_RES)
        for _ in range(4):
            self.cycle()
        self.assertEqual(self.net.puts, [], "JP residential and US datacenter nodes are healthy but off limits")

    def test_manual_country_change_relocks_and_routes_there(self):
        self.serve(friend_proxies())
        self.cycle()
        self.net.proxy_data[GROUP]["now"] = JP_RES[0]   # the friend picks Japan in Clash Verge
        self.cycle()
        self.assertEqual(router.POLICY_BY_GROUP[GROUP]["region"], "JP")
        self.assertEqual(router.GROUPS[GROUP], JP_RES)
        self.assertEqual(router.AUTO_LOCK_STATUS[GROUP]["status"], "relocked")
        marks = [p for p in router.TIMELINE.get(GROUP, []) if p.get("reason") == "manual"]
        self.assertEqual(marks[-1]["node"], JP_RES[0], "the chart marks the user's own switch")
        self.assertNotIn("failover_times", self.state["groups"][GROUP], "a manual switch is never a failover")
        self.net.down_nodes.add(JP_RES[0])
        self.cycle()
        self.assertEqual(self.net.puts, [(GROUP, JP_RES[1])])
        registry = self.state["candidate_registry"]["policies"]
        self.assertEqual(list(registry), [auto_lock._policy_id(GROUP, "JP")], "the US record is pruned")

    def test_router_switch_is_not_mistaken_for_a_manual_change(self):
        self.serve(friend_proxies())
        self.cycle()
        self.net.down_nodes.add(US_RES[0])
        self.cycle()
        self.cycle()
        self.assertEqual(router.AUTO_LOCK_STATUS[GROUP]["status"], "locked")
        self.assertEqual(self.state["auto_lock"][GROUP]["reason"], "detected")

    def test_non_residential_start_moves_onto_residential(self):
        self.serve(friend_proxies(now_main=US_DC[0]))
        self.cycle()
        self.assertEqual(self.net.puts, [(GROUP, US_RES[0])])
        self.assertEqual(self.state["groups"][GROUP]["adopt_switch_times"].__len__(), 1)
        self.assertNotIn("removal_switch_times", self.state["groups"][GROUP])

    def test_non_residential_start_waits_for_warm_residential_nodes(self):
        for name in US_RES:
            self.state["nodes"].pop(name)
        self.serve(friend_proxies(now_main=US_DC[0]))
        self.cycle()
        self.assertEqual(self.net.puts, [])
        self.assertIn(US_DC[0], {name for name, _url in self.net.probe_calls}, "the friend's node is watched")

    def test_manual_non_residential_pick_is_honoured_until_it_fails(self):
        self.serve(friend_proxies())
        self.cycle()
        self.net.proxy_data[GROUP]["now"] = US_DC[1]
        self.cycle()
        self.cycle()
        self.assertEqual(self.net.puts, [], "manual pick kept for 60 minutes")
        self.assertGreater(self.state["groups"][GROUP]["manual_hold_until"], self.now)
        self.net.down_nodes.add(US_DC[1])
        self.cycle()
        self.assertEqual(self.net.puts, [(GROUP, US_RES[0])])
        self.assertEqual(len(self.state["groups"][GROUP]["failover_times"]), 1)

    def test_group_moved_onto_another_group_is_paused_not_relocked(self):
        # Clash Verge reset the group to its first entry, which is the url-test group.
        self.serve(friend_proxies())
        self.cycle()
        self.net.proxy_data[GROUP]["now"] = "♻️ 自动选择"
        self.cycle()
        self.assertEqual((router.POLICIES, self.net.puts), ([], []))
        self.assertEqual(router.AUTO_LOCK_STATUS[GROUP]["status"], "paused")
        self.assertTrue(router.AUTO_LOCK_STATUS[GROUP]["current_is_auto_group"])
        self.assertEqual(self.state["auto_lock"][GROUP]["country"], "US")
        self.net.proxy_data[GROUP]["now"] = US_RES[1]
        self.cycle()
        self.assertEqual(router.AUTO_LOCK_STATUS[GROUP]["status"], "locked")

    def test_reset_to_another_country_first_entry_is_read_as_a_manual_change(self):
        # Known limitation: indistinguishable from the user picking that node.
        proxies = friend_proxies()
        proxies[GROUP]["all"] = [JP_RES[0]] + proxies[GROUP]["all"]
        self.serve(proxies)
        self.cycle()
        self.net.proxy_data[GROUP]["now"] = JP_RES[0]
        self.cycle()
        self.assertEqual(router.POLICY_BY_GROUP[GROUP]["region"], "JP")

    def test_country_without_residential_is_monitor_only(self):
        self.serve(friend_proxies(now_main=KR_DC[0]))
        for _ in range(3):
            self.cycle()
        self.assertEqual((router.POLICIES, self.net.puts), ([], []))
        self.assertEqual(router.AUTO_LOCK_STATUS[GROUP]["status"], "no_residential")
        snapshots = router.build_status_snapshots(self.state, self.net.proxy_data, [], now=int(self.now))
        self.assertEqual(snapshots["legacy"]["service"]["profile"], "auto_lock")

    def test_two_groups_on_the_same_country_share_candidates(self):
        self.serve(friend_proxies(now_ai=US_RES[2]))
        self.cycle()
        self.assertEqual(sorted(router.GROUPS), sorted([GROUP, AI_GROUP]))
        for group in (GROUP, AI_GROUP):
            for name in US_RES:
                self.assertTrue(router.selection_allowed(group, name), (group, name))

    def test_guard_refuses_cross_country_selection(self):
        self.serve(friend_proxies(now_ai=JP_RES[0]))
        self.cycle()
        for group, name in ((GROUP, JP_RES[0]), (AI_GROUP, US_RES[0]), (GROUP, US_DC[0]), (GROUP, "DIRECT")):
            with self.subTest(group=group, name=name):
                self.assertFalse(router.selection_allowed(group, name))
                with mock.patch.object(router, "log_warning"), self.assertRaises(router.RegionGuardError):
                    router.select_node(group, name, dry_run=True)

    def test_status_carries_lock_details(self):
        self.serve(friend_proxies(now_ai=JP_RES[0]))
        self.cycle()
        snapshots = router.build_status_snapshots(self.state, self.net.proxy_data, [], now=int(self.now))
        legacy = {group["name"]: group for group in snapshots["legacy"]["groups"]}
        self.assertEqual(legacy[GROUP]["region_label"], "美国")
        self.assertEqual(legacy[AI_GROUP]["auto_lock"]["country_label"], "日本")
        self.assertNotIn("♻️ 自动选择", snapshots["legacy"]["service"]["auto_lock_idle"], "url-test groups are not listed")

    def test_fixed_profile_is_untouched(self):
        fixed = cycle_harness.load_router("auto_lock_fixed_router")
        self.assertEqual(fixed.PROFILE, "fixed")
        before = list(fixed.GROUPS)
        fixed.refresh_auto_lock({}, friend_proxies(), self.now)
        self.assertEqual(list(fixed.GROUPS), before)


class RegionTests(unittest.TestCase):
    def test_common_airport_names(self):
        cases = {
            "🇺🇸 美国 家宽 01": ("US", True), "US Residential 02": ("US", True), "美国 洛杉矶 01": ("US", False),
            "JP-Tokyo-ISP": ("JP", True), "🇹🇼 台湾 HiNet 家宽": ("TW", True), "香港 HKT": ("HK", True),
            "SG ISP 1": ("SG", True), "英国 伦敦 住宅": ("GB", True), "新加坡 01": ("SG", False),
        }
        for name, (country, residential) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(regions.region_of(name)[0], country)
                self.assertEqual(regions.is_residential(name), residential)

    def test_isp_needs_word_boundaries(self):
        self.assertFalse(regions.is_residential("美国 CRISP 01"))


if __name__ == "__main__":
    unittest.main()
