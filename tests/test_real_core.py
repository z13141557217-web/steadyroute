"""Against a real Mihomo core (skipped unless STEADYROUTE_MIHOMO points at one; CI builds it).

Every other test talks to a controller written for this project. These check the things only the
core itself can answer: which files it is willing to open, what its config check prints, how its
filters read a node name, and how it reports groups and rules.
"""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402,F401  (puts src/steadyroute on sys.path)

import ai_line  # noqa: E402
import ai_rules  # noqa: E402
import auto_lock  # noqa: E402
import clash_profile  # noqa: E402
import node_catalog  # noqa: E402
import real_core  # noqa: E402
import regions  # noqa: E402
import settings_service  # noqa: E402

BASE_CONFIG = {
    "schema_version": 1, "mode": "shadow", "profile": "auto_lock",
    "auto_lock": {"exclude_groups": [], "business_test_urls": ["https://www.gstatic.com/generate_204"]},
    "policies": [],
}
# In the generated GeoSite.dat: openai and category-ai-!cn. Not in it: category-ntp. No ASN database.
GEOSITE = {"cn": ["example.cn"], "openai": ["openai.com", "chatgpt.com"], "category-ai-!cn": ["gemini.google.com"]}


class RequiredCoreTests(unittest.TestCase):
    def test_ci_never_passes_without_the_core(self):
        """In CI a missing or broken core must fail the run, not skip the tests that need it."""
        import os
        if os.environ.get("STEADYROUTE_REQUIRE_CORE"):
            self.assertTrue(real_core.available(), "STEADYROUTE_MIHOMO=%r is not an executable core" % real_core.CORE)


@unittest.skipUnless(real_core.available(), "no Mihomo core (set STEADYROUTE_MIHOMO; see scripts/build-core.sh)")
class RealCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = real_core.Core(GEOSITE)

    @classmethod
    def tearDownClass(cls):
        cls.core.stop()

    def setUp(self):
        self.core.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        (self.base / "config").mkdir()
        self.config_path = self.base / "config" / "route-policies.json"
        self.config_path.write_text(json.dumps(BASE_CONFIG), encoding="utf-8")
        self.clock = [1790409600.0]
        self.service = settings_service.SettingsService(
            self.config_path, self.base, self.core, clash_home=self.core.verge, core=real_core.CORE,
            fetch=lambda url: (_ for _ in ()).throw(OSError("offline")), clock=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def node_names(self):
        return [name for name, proxy in self.core.proxies().items() if ai_line.is_node(proxy)]

    # ------------------------------------------------------------ what the core is
    def test_version_passes_the_minimum_check(self):
        self.assertRegex(self.service.check_core(), r"^v?\d+\.\d+\.\d+")

    def test_builtin_outbounds_and_groups_are_never_taken_for_nodes(self):
        proxies = self.core.proxies()
        builtin = {name for name, proxy in proxies.items()
                   if "all" not in proxy and name not in real_core.NODES and proxy["type"] != "Socks5"}
        self.assertTrue({"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"} <= builtin, builtin)
        for name in sorted(builtin) + [name for name, proxy in proxies.items() if "all" in proxy]:
            proxy = proxies[name]
            self.assertFalse(ai_line.is_node(proxy), "%s (%s)" % (name, proxy["type"]))
            self.assertFalse(auto_lock.is_node(proxy), "%s (%s)" % (name, proxy["type"]))
            self.assertFalse(node_catalog._is_node(name, proxy), "%s (%s)" % (name, proxy["type"]))
        self.assertEqual(sorted(self.node_names()), sorted(set(real_core.NODES) | {"香港 家宽 01", "香港 BGP 01"}))

    # ------------------------------------------------------------ loading a config
    def test_core_opens_no_file_outside_its_home_and_takes_the_config_as_text(self):
        """What v0.5.1 ran into on a real Mac: Clash Verge's runtime file is not under the core's home."""
        runtime = self.core.verge / "clash-verge.yaml"
        status, body = self.core("PUT", "/configs?force=true", {"path": str(runtime)})
        self.assertEqual(status, 400)
        self.assertIn("path is not subpath of home directory or SAFE_PATHS", body)
        self.assertIn(str(self.core.home), body)
        writer = self.service.writer()
        writer._reload(runtime.read_text(encoding="utf-8").replace("- MATCH,", "- DOMAIN,only.example,DIRECT\n- MATCH,"))
        self.assertIn(("Domain", "only.example", "DIRECT"), self.core.rules())

    def test_refused_config_leaves_the_core_running_the_old_one(self):
        before = self.core.rules()
        with self.assertRaises(clash_profile.ReloadRejected) as caught:
            self.service.writer()._reload(self.core.original.replace("type: fallback", "type: nonsense"))
        self.assertIn("unsupported", str(caught.exception))
        self.assertEqual(self.core.rules(), before)

    def test_config_check_reports_the_cores_own_reason(self):
        validate = self.service.writer().validate
        validate(self.core.original)
        with self.assertRaises(clash_profile.ProfileError) as caught:
            validate(self.core.original.replace("- MATCH,", "- DOMAIN,x.example,没有这个分组\n- MATCH,"))
        self.assertIn("没有这个分组", str(caught.exception))
        self.assertNotIn("test failed", str(caught.exception))

    # ------------------------------------------------------------ filters
    def test_filters_pick_the_nodes_the_preview_promised(self):
        """The settings page lists members with Python's re; the core applies the same pattern with its own engine."""
        names = self.node_names()
        groups = [ai_line.group_definition("线路 %s" % code, code, True) for code, _label, _pattern in regions.REGIONS]
        text = clash_profile.patch_runtime(self.core.original, [clash_profile.group_item(group) for group in groups], [],
                                           {group["name"] for group in groups})
        self.service.writer().validate(text)
        self.assertEqual(self.core.load(text)[0], 204)
        proxies = self.core.proxies()
        picked = 0
        for code, label, _pattern in regions.REGIONS:
            expected = ai_line.members(code, names)
            got = proxies["线路 %s" % code]["all"]
            if expected:
                self.assertEqual(sorted(got), sorted(expected), "%s %s" % (code, label))
                picked += len(expected)
            else:   # nobody matches: the line refuses connections, it never falls back to DIRECT
                self.assertEqual(got, ["REJECT"], "%s %s" % (code, label))
        self.assertGreater(picked, 15)
        taiwan = proxies["线路 TW"]["all"]
        for right in ("【2x】优化线路|台湾hinet动态住宅A1", "[A2]台湾hinet住宅hy2", "🇹🇼 Taiwan Residential 03", "台灣 住宅 04"):
            self.assertIn(right, taiwan)
        for wrong in ("台湾 BGP 02", "🇹🇼 台湾 01", "台湾家宽 到期：2026-12-01", "香港 家宽 01"):
            self.assertNotIn(wrong, taiwan)

    # ------------------------------------------------------------ the AI line, end to end
    def apply(self, country="TW"):
        result = self.service.apply({"ai_line": {"enabled": True, "country": country}})
        return result, self.service.applied()

    def test_apply_puts_the_line_and_its_rules_first(self):
        user_rules = self.core.rules()
        result, applied = self.apply()
        # the core has no ASN database and no category-ntp list: exactly those two optional rules go
        self.assertEqual(sorted(map(tuple, result["dropped"])), [("GEOSITE", "category-ntp"), ("IP-ASN", "399358")])
        group = applied["groups"][0]
        self.assertEqual(self.core.proxies()[group]["type"], "Selector")
        self.assertEqual(sorted(self.core.proxies()[group]["all"]), sorted(ai_line.members("TW", self.node_names())))
        rules = self.core.rules()
        ours = rules[:len(applied["rules"])]
        self.assertEqual({proxy for _kind, _payload, proxy in ours}, {group})
        self.assertEqual(rules[len(applied["rules"]):], user_rules, "the user's rules follow, untouched")
        self.assertIn(("GeoSite", "category-ai-!cn", group), ours)
        self.assertIn(("IPCIDR", "2607:6bc0::/32", group), ours)
        self.assertEqual(self.service.line_problems(applied), "")
        self.assertIsNone(self.service.watch())
        self.assertEqual(self.service.line_status()["state"], "ok")

    def test_routing_check_reads_the_cores_rules(self):
        self.apply()
        report = self.service.check()
        missed = [row["rule"] for row in report["rows"] if not row["ok"]]
        self.assertEqual(missed, ["IP-ASN,399358"], "only the rule this core could not load")
        self.assertEqual((report["total"], report["line_country"]), (34, "台湾"))

    def test_overwritten_core_is_noticed_and_written_again_with_the_same_node(self):
        _result, applied = self.apply()
        group = applied["groups"][0]
        second = self.core.proxies()[group]["all"][1]
        self.assertEqual(self.core("PUT", "/proxies/" + __import__("urllib.parse").parse.quote(group, safe=""),
                                   {"name": second})[0], 204)
        written = (self.core.verge / "clash-verge.yaml").read_text(encoding="utf-8")
        # Clash Verge reloads the config it keeps in memory, and writes it out
        (self.core.verge / "clash-verge.yaml").write_text(self.core.original, encoding="utf-8")
        self.assertEqual(self.core.load(self.core.original)[0], 204)
        self.clock[0] += settings_service.WATCH_SECONDS
        self.assertIsNone(self.service.watch())
        status = self.service.line_status()
        self.assertEqual(status["state"], "missing")
        self.assertIn("Clash 中没有分组", status["problem"])
        self.assertIn("AI 规则", status["problem"])
        self.clock[0] += settings_service.WATCH_SECONDS
        self.assertEqual(self.service.watch(), "repaired")
        self.assertEqual(self.service.line_status()["state"], "ok")
        self.assertEqual((self.core.verge / "clash-verge.yaml").read_text(encoding="utf-8"), written)
        self.assertEqual(self.service.line_problems(self.service.applied()), "")

    def test_switching_the_line_off_puts_the_core_back(self):
        groups, rules = self.core.groups(), self.core.rules()
        files = {name: (self.core.verge / name).read_text(encoding="utf-8")
                 for name in ("profiles/gkX1aa.yaml", "profiles/rkX1aa.yaml")}
        self.apply("JP")
        self.assertNotEqual(self.core.rules(), rules)
        self.service.apply({"ai_line": {"enabled": False}})
        self.assertEqual(self.core.groups(), groups)
        self.assertEqual(self.core.rules(), rules)
        self.assertEqual({name: (self.core.verge / name).read_text(encoding="utf-8") for name in files}, files)
        self.assertEqual(self.service.line_status()["state"], "off")

    def test_country_without_a_single_node_is_refused_not_sent_direct(self):
        result, applied = self.apply("DE")
        group = applied["groups"][0]
        self.assertEqual(self.core.proxies()[group]["all"], ["REJECT"])
        self.assertEqual(self.core.proxies()[group]["now"], "REJECT")

    # ------------------------------------------------------------ the router's own HTTP client
    def test_router_client_reads_the_core_over_its_socket(self):
        import os
        router = cycle_harness.load_router("steadyroute_real_core_router")
        os.environ["STEADYROUTE_CONTROLLER_SOCKET"] = self.core.socket
        try:
            self.assertRegex(router.api_request("GET", "/version")["version"], r"\d+\.\d+\.\d+")
            self.assertIn("GLOBAL", router.api_request("GET", "/proxies")["proxies"])
            status, body = router.controller_call("PUT", "/proxies/%E4%B8%8D%E5%AD%98%E5%9C%A8", {"name": "x"})
            self.assertEqual(status, 404)
            self.assertNotIn("b'", body)
            self.assertEqual(router.controller_call("PUT", "/configs?force=true",
                                                    {"path": "", "payload": self.core.original})[0], 204)
        finally:
            del os.environ["STEADYROUTE_CONTROLLER_SOCKET"]


if __name__ == "__main__":
    unittest.main()
