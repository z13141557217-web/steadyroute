"""v0.5.1: the AI line and residential lines written into Clash Verge, and taken out again."""

import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402,F401  (puts src/steadyroute on sys.path)

import ai_line  # noqa: E402
import ai_rules  # noqa: E402
import clash_profile  # noqa: E402

try:
    import yaml
except ImportError:  # CI has no PyYAML; the structural checks below still run
    yaml = None

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "clash_verge"
TW_NODES = ["台湾 HiNet 家宽 02 🇨🇳", "台湾 HiNet 家宽 01 🇨🇳", "台湾 HiNet 家宽 04",
            "台湾 HiNet 家宽 10", "台湾 HiNet 家宽 11"]
OTHER_NODES = ["🇯🇵 日本 家宽 01", "香港 BGP 01", "香港 家宽 01", "香港 家宽 02",
               "🇹🇼 台湾 01", "剩余流量：100G", "台湾家宽 到期：2026-12-01"]


def proxies_payload(groups=None):
    data = {name: {"type": "Hysteria2"} for name in TW_NODES + OTHER_NODES}
    data["AI 台湾家宽线路"] = {"type": "Selector", "now": TW_NODES[0], "all": TW_NODES[:2]}
    data["香港家宽自动备援"] = {"type": "Selector", "now": "香港 家宽 01", "all": ["香港 家宽 01"]}
    for name in groups or []:
        data.setdefault(name, {"type": "Selector", "now": TW_NODES[0], "all": TW_NODES})
    return data


class FakeController(object):
    def __init__(self, test_case):
        self.calls = []
        self.fail_reload = False
        self.groups_after = None

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if method == "PUT" and path.startswith("/configs"):
            return (400, "bad config") if self.fail_reload else (204, "")
        if method == "GET" and path == "/proxies":
            return 200, json.dumps({"proxies": proxies_payload(self.groups_after)}, ensure_ascii=False)
        return 204, ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / "clash"
        shutil.copytree(str(FIXTURE), str(self.home))
        self.target = clash_profile.Target(self.home)
        self.controller = FakeController(self)
        self.validated = []
        self.reject = None

        def validate(text):
            self.validated.append(text)
            if yaml:
                yaml.safe_load(text)
            if self.reject and self.reject in text:
                raise clash_profile.ProfileError("rejected %s" % self.reject)
        self.writer = clash_profile.Writer(self.target, pathlib.Path(self.tmp.name) / "backups", validate, self.controller)

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, config, applied=None):
        self.controller.groups_after = [line["name"] for line in ai_line.Manager(
            config, {}, self.target, self.writer, lambda: {}).lines()]
        return ai_line.Manager(config, applied or {}, self.target, self.writer,
                               lambda: proxies_payload(self.controller.groups_after))

    def read(self, name):
        return (self.home / name).read_text(encoding="utf-8")


MIGRATED = {
    "ai_line": {"enabled": True, "group_name": "AI 台湾家宽线路", "country": "TW"},
    "managed_lines": [{"group_name": "香港家宽自动备援", "country": "HK"}],
    "legacy_group_names": ["SteadyRoute 发现·台湾家宽", "SteadyRoute 发现·香港家宽"],
    "ai_rules": {"manual": ["gemini.google.com"]},
}


class FilterTests(unittest.TestCase):
    def test_filter_picks_this_countrys_residential_nodes_only(self):
        self.assertEqual(ai_line.members("TW", TW_NODES + OTHER_NODES), TW_NODES)
        self.assertEqual(ai_line.members("HK", TW_NODES + OTHER_NODES), ["香港 家宽 01", "香港 家宽 02"])
        self.assertEqual(ai_line.members("JP", TW_NODES + OTHER_NODES), ["🇯🇵 日本 家宽 01"])

    def test_group_never_falls_back_to_direct(self):
        group = ai_line.group_definition("AI 家宽专线", "TW", ai=True)
        self.assertEqual(group["empty-fallback"], "REJECT")   # Mihomo's default COMPATIBLE is a direct connection
        self.assertNotIn("disable-udp", group, "net.coffee: UDP goes through the proxy too")
        self.assertTrue(group["include-all-proxies"])
        self.assertNotIn("disable-udp", ai_line.group_definition("香港家宽", "HK", ai=False))

    def test_country_choices_mark_ai_unsupported_regions(self):
        rows = ai_line.country_choices(proxies_payload())
        by = {row["code"]: row for row in rows}
        self.assertEqual(by["TW"]["residential"], 5)
        self.assertFalse(by["HK"]["ai_ok"])
        self.assertTrue(by["JP"]["ai_ok"])
        self.assertEqual(rows[0]["code"], "TW", "most residential nodes first among AI-capable countries")
        self.assertEqual(rows[-1]["code"], "HK")


class RulesTests(unittest.TestCase):
    def test_netcoffee_rules_come_first_including_ntp(self):
        lines, breakdown = ai_rules.build("AI 家宽专线", manual=[ai_rules.manual_entry("https://gemini.google.com/app")])
        self.assertEqual(lines[0], "DOMAIN-SUFFIX,anthropic.com,AI 家宽专线")
        self.assertIn("IP-CIDR,160.79.104.0/21,AI 家宽专线,no-resolve", lines)
        self.assertIn("IP-ASN,399358,AI 家宽专线,no-resolve", lines)
        self.assertIn("GEOSITE,openai,AI 家宽专线", lines)
        self.assertIn("GEOSITE,category-ai-!cn,AI 家宽专线", lines)
        self.assertEqual(lines[-1], "DOMAIN-SUFFIX,gemini.google.com,AI 家宽专线")
        self.assertEqual(lines[lines.index("IP-ASN,399358,AI 家宽专线,no-resolve") + 1], "GEOSITE,category-ntp,AI 家宽专线")
        self.assertEqual([b["count"] for b in breakdown], [23, 13, 1, len(ai_rules.AI_PROCESSES), 1])
        self.assertLess(lines.index("DOMAIN-SUFFIX,chatgpt.com,AI 家宽专线"),
                        lines.index("GEOSITE,category-ai-!cn,AI 家宽专线"))

    def test_page_parser_reads_rules_from_html(self):
        page = """<h2>Clash</h2><pre><code>payload:
  - DOMAIN-SUFFIX,anthropic.com
  - DOMAIN-SUFFIX,claude.ai
  - DOMAIN-KEYWORD,datadog
  - IP-CIDR,160.79.104.0/21,no-resolve
  - IP-ASN,399358,no-resolve
  - GEOSITE,category-ntp
</code></pre><p>rules:<br>- DOMAIN-SUFFIX,chatgpt.com,🚀 代理出口</p>"""
        entries = ai_rules.parse_page(page)
        self.assertIn(("IP-ASN", "399358"), entries)
        self.assertIn(("DOMAIN-SUFFIX", "chatgpt.com"), entries)
        self.assertIn(("GEOSITE", "category-ntp"), entries)

    def test_unsafe_sync_is_refused(self):
        good = list(ai_rules.NETCOFFEE_CLAUDE)
        ai_rules.check_source("claude", good, previous=good)
        for bad in (good + [("DOMAIN-KEYWORD", "com")], good + [("IP-CIDR", "0.0.0.0/0")],
                    good + [("DOMAIN-SUFFIX", "com")], good[2:], good[:3] + [("DOMAIN-SUFFIX", "anthropic.com")]):
            with self.subTest(bad=bad[-1]):
                with self.assertRaises(ai_rules.RuleSourceError):
                    ai_rules.check_source("claude", [e for e in bad if ai_rules.valid_entry(*e)] + (
                        [bad[-1]] if not ai_rules.valid_entry(*bad[-1]) else []), previous=good)

    def test_ai_line_tests_web_and_api_even_after_migration(self):
        migrated = ["https://chatgpt.com/cdn-cgi/trace", "https://claude.ai/cdn-cgi/trace"]
        urls = ai_line.business_urls(migrated)
        self.assertEqual(urls, ai_line.AI_BUSINESS_URLS)
        self.assertIn("https://api.openai.com/cdn-cgi/trace", urls)
        self.assertIn("https://api.anthropic.com/v1/models", urls)
        self.assertEqual(ai_line.business_urls(["https://example.com/x"])[-1], "https://example.com/x")

    def test_manual_entries_are_cleaned_or_refused(self):
        self.assertEqual(ai_rules.manual_entry(" *.Perplexity.AI/ "), ("DOMAIN-SUFFIX", "perplexity.ai"))
        self.assertEqual(ai_rules.manual_entry("https://www.perplexity.ai/search?q=1"), ("DOMAIN-SUFFIX", "perplexity.ai"))
        self.assertEqual(ai_rules.manual_entry("www.io"), ("DOMAIN-SUFFIX", "www.io"))
        self.assertIsNone(ai_rules.manual_entry("com"))
        self.assertIsNone(ai_rules.manual_entry("bad domain!"))


class ProfileTests(Base):
    def test_bindings_follow_the_current_subscription(self):
        uid, groups, rules = self.target.resolve()
        self.assertEqual((uid, groups.name, rules.name), ("RbX7kQ2mN0aa", "gkX1aa.yaml", "rkX1aa.yaml"))

    def test_bindings_with_indented_items(self):
        text = "current: A\nitems:\n  - uid: A\n    type: remote\n    option:\n      groups: g1\n      rules: r1\n  - uid: g1\n    type: groups\n"
        current, items = clash_profile.profile_bindings(text)
        self.assertEqual((current, items["A"]["option"]), ("A", {"groups": "g1", "rules": "r1"}))

    def test_users_own_prepend_rules_stay_after_our_block(self):
        text = "prepend:\n  - DOMAIN-SUFFIX,mine.example,DIRECT\nappend: []\n"
        edited, _removed = clash_profile.edit_prepend(text, ['"DOMAIN,a.example,X"'])
        self.assertLess(edited.index("a.example"), edited.index("mine.example"))
        self.assertEqual(clash_profile.restore_prepend(edited, []), text)

    def test_edit_and_restore_leave_everything_else_untouched(self):
        original = self.read("profiles/rkX1aa.yaml")
        edited, removed = clash_profile.edit_prepend(original, ['"DOMAIN,a.example,X"'])
        self.assertIn(clash_profile.MARK_BEGIN, edited)
        self.assertEqual(removed, [])
        self.assertEqual(clash_profile.restore_prepend(edited, []), original)

    def test_legacy_groups_are_replaced_and_put_back(self):
        original = self.read("profiles/gkX1aa.yaml")
        edited, removed = clash_profile.edit_prepend(
            original, [clash_profile.group_item(ai_line.group_definition("AI 台湾家宽线路", "TW", True))],
            {"AI 台湾家宽线路", "SteadyRoute 发现·台湾家宽"})
        self.assertEqual(len(removed), 2)
        self.assertIn("自定义稳定线路", edited, "the user's own groups stay")
        if yaml:
            data = yaml.safe_load(edited)
            self.assertEqual([g["name"] for g in data["prepend"]], ["AI 台湾家宽线路", "自定义稳定线路"])
            self.assertEqual(data["prepend"][0]["empty-fallback"], "REJECT")
        restored = clash_profile.restore_prepend(edited, removed)
        if yaml:
            self.assertEqual(yaml.safe_load(restored), yaml.safe_load(original))

    def test_runtime_patch_puts_ours_first_and_keeps_the_users(self):
        runtime = self.read("clash-verge.yaml")
        group = clash_profile.group_item(ai_line.group_definition("AI 台湾家宽线路", "TW", True))
        rules = [clash_profile.rule_item("DOMAIN-SUFFIX,clau.de,AI 台湾家宽线路"),
                 clash_profile.rule_item("DOMAIN-SUFFIX,claude.ai,AI 台湾家宽线路")]
        patched = clash_profile.patch_runtime(runtime, [group], rules, {"AI 台湾家宽线路", "SteadyRoute 发现·台湾家宽"})
        again = clash_profile.patch_runtime(patched, [group], rules, {"AI 台湾家宽线路", "SteadyRoute 发现·台湾家宽"})
        self.assertEqual(patched, again, "applying twice changes nothing")
        if yaml:
            data = yaml.safe_load(patched)
            self.assertEqual([g["name"] for g in data["proxy-groups"]],
                             ["AI 台湾家宽线路", "自定义稳定线路", "家宽出口", "🐟 漏网之鱼"])
            self.assertEqual(data["rules"][:2], ["DOMAIN-SUFFIX,clau.de,AI 台湾家宽线路", "DOMAIN-SUFFIX,claude.ai,AI 台湾家宽线路"])
            self.assertIn("MATCH,🐟 漏网之鱼", data["rules"])
            self.assertEqual(data["rules"].count("DOMAIN-SUFFIX,claude.ai,AI 台湾家宽线路"), 1)
            self.assertEqual(len(data["proxies"]), 5)

    def test_flow_style_prepend_is_refused(self):
        with self.assertRaises(clash_profile.ProfileError):
            clash_profile.edit_prepend("prepend: [a, b]\n", ['"x"'])


class ManagerTests(Base):
    def test_migration_apply_writes_both_lines_and_all_rules(self):
        manager = self.manager(MIGRATED)
        plan = manager.plan()
        tw = plan["lines"][0]
        self.assertEqual(tw["added"], TW_NODES[2:], "the preview shows nodes the filter adds")
        self.assertEqual(sorted(plan["removed_legacy"]), sorted(["AI 台湾家宽线路", "SteadyRoute 发现·台湾家宽"]))
        plan, applied = manager.apply()
        groups = self.read("profiles/gkX1aa.yaml")
        rules = self.read("profiles/rkX1aa.yaml")
        runtime = self.read("clash-verge.yaml")
        self.assertIn("自定义稳定线路", groups)
        self.assertEqual(rules.count("AI 台湾家宽线路"), len(plan["rules"]))
        self.assertIn("gemini.google.com", rules)
        if yaml:
            data = yaml.safe_load(runtime)
            self.assertEqual(data["rules"][0], "DOMAIN-SUFFIX,anthropic.com,AI 台湾家宽线路")
            self.assertEqual([g["name"] for g in data["proxy-groups"]][:2], ["AI 台湾家宽线路", "香港家宽自动备援"])
            self.assertEqual(yaml.safe_load(groups)["delete"], [])
        self.assertEqual(self.controller.calls[-1][0], "PUT", "the previous selection is restored")
        self.assertEqual(applied["groups"], ["AI 台湾家宽线路", "香港家宽自动备援"])
        self.assertTrue(manager.healthy())

    def test_optional_rules_are_dropped_when_the_core_lacks_their_database(self):
        self.reject = "category-ai-!cn"
        plan, applied = self.manager(MIGRATED).apply()
        self.assertEqual(applied["dropped"], [["GEOSITE", "category-ai-!cn"]])
        self.assertNotIn("category-ai-!cn", self.read("clash-verge.yaml"))

    def test_failed_reload_restores_every_file(self):
        before = {name: self.read(name) for name in ("clash-verge.yaml", "profiles/gkX1aa.yaml", "profiles/rkX1aa.yaml")}
        self.controller.fail_reload = True
        with self.assertRaises(clash_profile.ProfileError):
            self.manager(MIGRATED).apply()
        self.assertEqual({name: self.read(name) for name in before}, before)

    def test_disable_puts_clash_back(self):
        before = {name: self.read(name) for name in ("profiles/gkX1aa.yaml", "profiles/rkX1aa.yaml")}
        manager = self.manager(MIGRATED)
        _plan, applied = manager.apply()
        self.controller.groups_after = None
        after = ai_line.Manager(MIGRATED, applied, self.target, self.writer, lambda: proxies_payload()).disable()
        self.assertEqual(after, {})
        if yaml:
            for name, text in before.items():
                self.assertEqual(yaml.safe_load(self.read(name)), yaml.safe_load(text), name)
            runtime = yaml.safe_load(self.read("clash-verge.yaml"))
            self.assertNotIn("DOMAIN-SUFFIX,clau.de,AI 台湾家宽线路", runtime["rules"])
            self.assertIn("AI 台湾家宽线路", [g["name"] for g in runtime["proxy-groups"]], "the old group is back")
        self.assertNotIn(clash_profile.MARK_BEGIN, self.read("profiles/rkX1aa.yaml"))

    def test_new_user_line_and_unsupported_country(self):
        config = {"ai_line": {"enabled": True, "group_name": "AI 家宽专线", "country": "HK"}}
        with self.assertRaises(clash_profile.ProfileError):
            self.manager(config).apply()
        config["ai_line"]["country"] = "JP"
        _plan, applied = self.manager(config).apply()
        self.assertEqual(applied["groups"], ["AI 家宽专线"])
        self.assertIn("SteadyRoute 发现·台湾家宽", self.read("profiles/gkX1aa.yaml"), "nothing of a new user's is removed")

    def test_switching_subscription_is_detected(self):
        manager = self.manager(MIGRATED)
        manager.apply()
        text = self.read("profiles.yaml").replace("current: RbX7kQ2mN0aa", "current: Other")
        (self.home / "profiles.yaml").write_text(text + "- uid: Other\n  type: remote\n  option:\n    rules: rkX1aa\n    groups: gkX1aa\n", encoding="utf-8")
        self.assertFalse(manager.healthy())


if __name__ == "__main__":
    unittest.main()
