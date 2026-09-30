"""v0.5.1 settings page backend: preview, apply, disable, weekly sync, AI routing check, and the
guard that only our own page can change anything."""

import http.client
import json
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

import ai_rules  # noqa: E402
import clash_profile  # noqa: E402
import settings_service  # noqa: E402

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "clash_verge"
NODES = ["台湾 HiNet 家宽 02 🇨🇳", "台湾 HiNet 家宽 01 🇨🇳", "台湾 HiNet 家宽 11",
         "🇯🇵 日本 家宽 01", "香港 家宽 01", "香港 BGP 01"]
BASE_CONFIG = {
    "schema_version": 1, "mode": "shadow", "profile": "auto_lock",
    "auto_lock": {"exclude_groups": [], "business_test_urls": ["https://www.gstatic.com/generate_204"]},
    "policies": [],
}


class FakeClash(object):
    def __init__(self):
        self.calls = []
        self.extra_groups = []
        self.rules = [{"type": "DomainSuffix", "payload": "claude.ai", "proxy": "AI 台湾家宽线路"},
                      {"type": "Match", "payload": "", "proxy": "🐟 漏网之鱼"}]

    def proxies(self):
        data = {name: {"type": "Hysteria2"} for name in NODES}
        data["AI 台湾家宽线路"] = {"type": "Selector", "now": NODES[0], "all": NODES[:2]}
        data["🐟 漏网之鱼"] = {"type": "Selector", "now": "家宽出口", "all": ["家宽出口"]}
        data["家宽出口"] = {"type": "Selector", "now": NODES[4], "all": NODES[3:5]}
        for name in self.extra_groups:
            data[name] = {"type": "Selector", "now": NODES[3], "all": [NODES[3]]}
        return data

    def __call__(self, method, path, payload):
        self.calls.append((method, path))
        if method == "GET" and path == "/proxies":
            return 200, json.dumps({"proxies": self.proxies()}, ensure_ascii=False)
        if method == "GET" and path == "/configs":
            return 200, json.dumps({"ipv6": False, "tun": {"enable": True}})
        if method == "GET" and path == "/rules":
            return 200, json.dumps({"rules": self.rules}, ensure_ascii=False)
        return 204, ""


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.clash_home = root / "clash"
        shutil.copytree(str(FIXTURE), str(self.clash_home))
        self.base = root / "app"
        (self.base / "config").mkdir(parents=True)
        self.config_path = self.base / "config" / "route-policies.json"
        self.config_path.write_text(json.dumps(BASE_CONFIG), encoding="utf-8")
        self.clash = FakeClash()
        self.pages = {}
        self.clock = [1790409600.0]

        def fetch(url):
            if url not in self.pages:
                raise OSError("offline")
            return self.pages[url]
        self.service = settings_service.SettingsService(
            self.config_path, self.base, self.clash, clash_home=self.clash_home, core="/bin/true",
            fetch=fetch, clock=lambda: self.clock[0], validator=lambda text: None)

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return {name: (self.clash_home / name).read_text(encoding="utf-8")
                for name in ("clash-verge.yaml", "profiles/gkX1aa.yaml", "profiles/rkX1aa.yaml")}


class SettingsServiceTests(ServiceBase):
    def test_snapshot_lists_countries_groups_and_clash_state(self):
        snap = self.service.snapshot()
        self.assertEqual(snap["countries"][0]["code"], "TW")
        self.assertFalse({row["code"]: row for row in snap["countries"]}["HK"]["ai_ok"])
        self.assertIn("AI 台湾家宽线路", snap["groups"])
        self.assertTrue(snap["clash"]["ok"])
        self.assertIs(snap["ipv6"], False)
        self.assertEqual(snap["rules_source"]["name"], "ip.net.coffee")

    def test_preview_changes_nothing(self):
        before = self.files()
        plan = self.service.preview({"ai_line": {"enabled": True, "country": "JP"}})
        self.assertEqual(plan["action"], "apply")
        self.assertEqual(plan["lines"][0]["after"], ["🇯🇵 日本 家宽 01"])
        self.assertGreater(plan["rule_count"], 40)
        self.assertNotIn("texts", plan)
        self.assertEqual(self.files(), before)

    def test_enable_then_disable(self):
        before = self.files()
        self.clash.extra_groups = ["AI 家宽专线"]
        result = self.service.apply({"ai_line": {"enabled": True, "country": "JP"}, "manual": ["gemini.google.com"]})
        self.assertEqual(result["action"], "apply")
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual((saved["ai_line"]["country"], saved["ai_rules"]["manual"]), ("JP", ["gemini.google.com"]))
        self.assertIn("AI 家宽专线", self.files()["profiles/gkX1aa.yaml"])
        self.assertTrue(self.service.applied()["groups"])
        result = self.service.apply({"ai_line": {"enabled": False}})
        self.assertEqual(result["action"], "disable")
        self.assertEqual(self.service.applied(), {})
        after = self.files()
        self.assertEqual(after["profiles/rkX1aa.yaml"], before["profiles/rkX1aa.yaml"])
        self.assertEqual(after["profiles/gkX1aa.yaml"], before["profiles/gkX1aa.yaml"])

    def test_exclusion_only_does_not_touch_clash(self):
        before = self.files()
        result = self.service.apply({"exclude_groups": ["家宽出口"]})
        self.assertFalse(result["clash_change"])
        self.assertEqual(self.files(), before)
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["auto_lock"]["exclude_groups"], ["家宽出口"])

    def test_bad_input_changes_nothing(self):
        before = (self.config_path.read_text(encoding="utf-8"), self.files())
        for changes in ({"manual": ["not a domain"]}, {"ai_line": {"enabled": True, "country": "HK"}},
                        {"ai_line": {"enabled": True, "country": "XX"}}):
            with self.subTest(changes=changes):
                with self.assertRaises((clash_profile.ProfileError, Exception)):
                    self.service.apply(changes)
                self.assertEqual((self.config_path.read_text(encoding="utf-8"), self.files()), before)

    def test_weekly_sync_updates_rules_and_reapplies(self):
        self.clash.extra_groups = ["AI 家宽专线"]
        self.service.apply({"ai_line": {"enabled": True, "country": "JP"}})
        claude_page = "<pre>" + "\n".join("- %s,%s" % item for item in ai_rules.NETCOFFEE_CLAUDE) + \
            "\n- DOMAIN-SUFFIX,claude-new.example.com</pre>"
        gpt_page = "<pre>" + "\n".join("- %s,%s,🚀 代理出口" % item for item in ai_rules.NETCOFFEE_GPT) + "</pre>"
        self.pages = {ai_rules.NETCOFFEE_CLAUDE_URL: claude_page, ai_rules.NETCOFFEE_GPT_URL: gpt_page}
        self.clock[0] += 8 * 86400
        self.service.maintenance()
        state = self.service.rules_state()
        self.assertEqual(state["last_change"]["added"], [["DOMAIN-SUFFIX", "claude-new.example.com"]])
        self.assertIn("claude-new.example.com", self.files()["profiles/rkX1aa.yaml"])
        self.assertIn(("POST", "/configs/geo"), self.clash.calls)
        self.assertTrue(state["geo_updated_at"])

    def test_failed_sync_keeps_the_rules_in_use(self):
        self.clock[0] += 8 * 86400
        self.service.maintenance()
        state = self.service.rules_state()
        self.assertIn("offline", state["last_error"])
        self.assertNotIn("claude", state)
        self.pages = {ai_rules.NETCOFFEE_CLAUDE_URL: "<pre>- DOMAIN-KEYWORD,com</pre>",
                      ai_rules.NETCOFFEE_GPT_URL: "<pre></pre>"}
        self.clock[0] += 8 * 86400
        self.service.maintenance()
        self.assertIn("claude:", self.service.rules_state()["last_error"])
        self.assertNotIn("claude", self.service.rules_state())

    def write_migration(self):
        config = dict(BASE_CONFIG, migration={
            "from": "fixed", "at": 1,
            "ai_line": {"enabled": True, "group_name": "AI 台湾家宽线路", "country": "TW"},
            "managed_lines": [{"group_name": "家宽出口", "country": "JP"}],
            "legacy_group_names": ["SteadyRoute 发现·台湾家宽"]})
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def test_migration_is_suggested_until_accepted(self):
        self.write_migration()
        self.assertEqual(self.service.snapshot()["migration"]["ai_line"]["group_name"], "AI 台湾家宽线路")
        plan = self.service.preview({"migration": "accept"})
        self.assertEqual([line["group"] for line in plan["lines"]], ["AI 台湾家宽线路", "家宽出口"])
        self.assertEqual(sorted(plan["removed_legacy"]), ["AI 台湾家宽线路", "SteadyRoute 发现·台湾家宽"])
        self.assertIn("migration", json.loads(self.config_path.read_text(encoding="utf-8")))
        result = self.service.apply({"migration": "accept"})
        self.assertEqual(result["groups"], ["AI 台湾家宽线路", "家宽出口"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("migration", saved)
        self.assertEqual(saved["ai_line"]["country"], "TW")
        self.assertEqual(saved["managed_lines"], [{"group_name": "家宽出口", "country": "JP"}])
        groups = self.files()["profiles/gkX1aa.yaml"]
        self.assertNotIn("SteadyRoute 发现·台湾家宽", groups)
        self.assertIn("自定义稳定线路", groups)

    def test_migration_can_be_dismissed(self):
        self.write_migration()
        before = self.files()
        result = self.service.apply({"migration": "dismiss"})
        self.assertFalse(result["clash_change"])
        self.assertNotIn("migration", json.loads(self.config_path.read_text(encoding="utf-8")))
        self.assertEqual(self.files(), before)

    def test_switching_the_ai_line_off_puts_the_old_group_back(self):
        """After migrating, the AI group has the old name the user's rules point at; switching
        the line off must not leave those rules without a group."""
        original = self.files()["profiles/gkX1aa.yaml"]
        old_definition = original[original.index("  - name: AI 台湾家宽线路"):original.index("  - name: SteadyRoute 发现")]
        self.write_migration()
        self.service.apply({"migration": "accept"})
        plan = self.service.preview({"ai_line": {"enabled": False}})
        self.assertEqual((plan["action"], plan["restored"], plan["rule_count"]), ("apply", ["AI 台湾家宽线路"], 0))
        self.clash.extra_groups = ["AI 台湾家宽线路"]
        result = self.service.apply({"ai_line": {"enabled": False}})
        self.assertEqual(result["groups"], ["家宽出口"])
        files = self.files()
        self.assertIn(old_definition, files["profiles/gkX1aa.yaml"])
        self.assertNotIn("AI 台湾家宽线路", files["profiles/rkX1aa.yaml"])
        self.assertEqual(files["clash-verge.yaml"].count("name: AI 台湾家宽线路"), 1)
        self.assertNotIn('"name": "AI 台湾家宽线路"', files["clash-verge.yaml"])
        self.assertEqual(sum("AI 台湾家宽线路" in item for item in self.service.applied()["removed_items"]), 0)
        # on again: the old definition is taken out once more, and remove_all still restores everything
        self.service.apply({"ai_line": {"enabled": True, "country": "TW"}})
        files = self.files()
        self.assertNotIn(old_definition, files["profiles/gkX1aa.yaml"])
        self.assertEqual(sum("AI 台湾家宽线路" in item for item in self.service.applied()["removed_items"]), 1)
        self.service.remove_all()
        self.assertEqual(self.files()["profiles/gkX1aa.yaml"], original)

    def test_unknown_setting_is_refused(self):
        with self.assertRaises(clash_profile.ProfileError):
            self.service.apply({"mode": "active"})

    def test_remove_all_puts_clash_back(self):
        before = self.files()
        self.write_migration()
        self.service.apply({"migration": "accept"})
        self.assertNotEqual(self.files(), before)
        self.assertEqual(self.service.remove_all(), ["AI 台湾家宽线路", "家宽出口"])
        after = self.files()
        self.assertEqual(after["profiles/gkX1aa.yaml"], before["profiles/gkX1aa.yaml"])
        self.assertEqual(after["profiles/rkX1aa.yaml"], before["profiles/rkX1aa.yaml"])
        self.assertEqual(self.service.applied(), {})
        self.assertFalse(json.loads(self.config_path.read_text(encoding="utf-8"))["ai_line"]["enabled"])
        self.assertEqual(self.service.remove_all(), [])

    def pages_ok(self, extra=""):
        claude_page = "<pre>" + "\n".join("- %s,%s" % item for item in ai_rules.NETCOFFEE_CLAUDE) + extra + "</pre>"
        gpt_page = "<pre>" + "\n".join("- %s,%s" % item for item in ai_rules.NETCOFFEE_GPT) + "</pre>"
        return {ai_rules.NETCOFFEE_CLAUDE_URL: claude_page, ai_rules.NETCOFFEE_GPT_URL: gpt_page}

    def test_failed_sync_is_retried_in_hours_not_a_week(self):
        calls = []
        original = self.service.fetch
        self.service.fetch = lambda url: calls.append(url) or original(url)
        start = self.clock[0]
        self.service.maintenance()                        # first run: offline
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.service.rules_state()["next_check_at"], int(start + 6 * 3600))
        self.clock[0] = start + 5 * 3600
        self.service.maintenance()
        self.assertEqual(len(calls), 1, "not due yet")
        for hours in (6, 12):                             # second and third failure
            self.clock[0] = start + hours * 3600
            self.service.maintenance()
        self.assertEqual(self.service.rules_state()["failures"], 3)
        self.assertEqual(self.service.rules_state()["next_check_at"], int(start + 12 * 3600 + 86400))
        self.pages = self.pages_ok()
        self.clock[0] = start + 12 * 3600 + 86400
        self.service.maintenance()
        state = self.service.rules_state()
        self.assertEqual((state["failures"], state["last_error"]), (0, None))
        self.assertEqual(state["next_check_at"], int(self.clock[0] + 7 * 86400))
        snap = self.service.snapshot()["rules_source"]
        self.assertEqual(snap["next_check_at"], state["next_check_at"])

    def test_sync_now_writes_new_rules_into_clash(self):
        self.clash.extra_groups = ["AI 家宽专线"]
        self.service.apply({"ai_line": {"enabled": True, "country": "JP"}})
        self.pages = self.pages_ok("\n- DOMAIN-SUFFIX,claude-new.example.com")
        result = self.service.sync_now()
        self.assertEqual((result["ok"], result["changed"]), (True, True))
        self.assertIn("claude-new.example.com", self.files()["profiles/rkX1aa.yaml"])
        self.pages = {}
        result = self.service.sync_now()
        self.assertFalse(result["ok"])
        self.assertIn("offline", result["error"])
        self.assertIn("claude-new.example.com", self.files()["profiles/rkX1aa.yaml"], "a failed sync keeps the rules")

    def test_group_details_show_country_and_state(self):
        self.config_path.write_text(json.dumps(dict(BASE_CONFIG, auto_lock={
            "exclude_groups": ["家宽出口"], "business_test_urls": ["https://www.gstatic.com/generate_204"]})), encoding="utf-8")
        rows = {row["name"]: row for row in self.service.snapshot()["group_details"]}
        self.assertEqual((rows["AI 台湾家宽线路"]["country_label"], rows["AI 台湾家宽线路"]["status"]), ("台湾", "switching"))
        self.assertEqual(rows["AI 台湾家宽线路"]["residential"], 2, "only the group's own members count")
        self.assertEqual(rows["家宽出口"]["status"], "excluded")

    def test_ai_check_reports_where_each_domain_goes(self):
        (self.config_path).write_text(json.dumps(dict(BASE_CONFIG, ai_line={
            "enabled": True, "group_name": "AI 台湾家宽线路", "country": "TW"})), encoding="utf-8")
        report = self.service.check()
        rows = {row["rule"]: row for row in report["rows"]}
        self.assertTrue(rows["DOMAIN-SUFFIX,claude.ai"]["ok"])
        self.assertEqual(rows["DOMAIN-SUFFIX,claude.ai"]["exit_country"], "台湾")
        self.assertFalse(rows["DOMAIN-SUFFIX,clau.de"]["ok"])
        self.assertEqual(rows["DOMAIN-SUFFIX,clau.de"]["exit_country"], "香港")
        self.assertEqual(rows["DOMAIN-SUFFIX,clau.de"]["chain"], ["🐟 漏网之鱼", "家宽出口", "香港 家宽 01"])
        self.assertEqual(report["total"], 34)


class AiCheckTests(unittest.TestCase):
    def test_asn_row_needs_the_asn_rule_and_same_target_unknowns_are_not_reported(self):
        import ai_check
        line = "AI 家宽专线"
        proxies = {line: {"type": "Selector", "now": "🇯🇵 日本 家宽 01"}, "🇯🇵 日本 家宽 01": {"type": "Socks5"},
                   "出口": {"type": "Selector", "now": "香港 家宽 01"}, "香港 家宽 01": {"type": "Socks5"}}
        rules = [{"type": "AND", "payload": "((Network,udp),(DstPort,3478-3481))", "proxy": "REJECT"},
                 {"type": "IPCIDR", "payload": "160.79.104.0/21", "proxy": line},
                 {"type": "GeoSite", "payload": "openai", "proxy": line},
                 {"type": "DomainSuffix", "payload": "openai.com", "proxy": line},
                 {"type": "Match", "payload": "", "proxy": "出口"}]
        rows = {row["rule"]: row for row in ai_check.run(rules, proxies, "", "/nonexistent", line, "JP")["rows"]}
        self.assertTrue(rows["IP-CIDR,160.79.104.0/21"]["ok"])
        self.assertFalse(rows["IP-ASN,399358"]["ok"])
        self.assertEqual(rows["IP-ASN,399358"]["hit"], "MATCH")
        self.assertEqual(rows["DOMAIN-SUFFIX,openai.com"]["unsure"], [])
        self.assertEqual(rows["DOMAIN,anthropic-com.ghost.io"]["unsure"], ["第 3 条 GeoSite,openai"])
        rules.insert(1, {"type": "IPASN", "payload": "399358", "proxy": line})
        rows = {row["rule"]: row for row in ai_check.run(rules, proxies, "", "/nonexistent", line, "JP")["rows"]}
        self.assertEqual(rows["IP-ASN,399358"]["hit"], "IP-ASN,399358")


class SettingsHttpTests(ServiceBase):
    def setUp(self):
        super().setUp()
        self.router = cycle_harness.load_router("settings_http_router")
        self.router.SETTINGS = self.service
        self.router.POLICY_CONFIG_PATH = str(self.config_path)
        self.server = self.router.QuietHTTPServer(("127.0.0.1", 0), self.router.DashboardHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def post(self, body, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/api/settings/apply", body=json.dumps(body).encode(), headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def good_headers(self):
        return {"Host": "127.0.0.1:%d" % self.port, "Origin": "http://127.0.0.1:%d" % self.port,
                "Content-Type": "application/json", "X-SteadyRoute": "1"}

    def test_only_our_page_can_change_settings(self):
        body = {"exclude_groups": ["家宽出口"]}
        for drop, change in (("Origin", None), ("X-SteadyRoute", None), (None, ("Origin", "http://evil.example")),
                             (None, ("Content-Type", "text/plain")), (None, ("Host", "evil.example:%d" % self.port))):
            headers = self.good_headers()
            if drop:
                headers.pop(drop)
            if change:
                headers[change[0]] = change[1]
            with self.subTest(drop=drop, change=change):
                status, _ = self.post(body, headers)
                self.assertIn(status, (403, 421))
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["auto_lock"]["exclude_groups"], [])
        status, data = self.post(body, self.good_headers())
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["auto_lock"]["exclude_groups"], ["家宽出口"])

    def test_sync_now_needs_our_page_too(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = self.good_headers()
        headers.pop("X-SteadyRoute")
        connection.request("POST", "/api/settings/sync", body=b"{}", headers=headers)
        self.assertEqual(connection.getresponse().status, 403)
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/api/settings/sync", body=b"{}", headers=self.good_headers())
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(response.read())["ok"])      # offline in the test

    def test_get_endpoints(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/api/settings", headers={"Host": "127.0.0.1:%d" % self.port})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("countries", json.loads(response.read()))
        self.assertEqual(response.getheader("Content-Security-Policy"), self.router.API_CSP)


if __name__ == "__main__":
    unittest.main()
