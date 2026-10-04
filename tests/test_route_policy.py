import pathlib
import copy
import sys
import unittest


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
sys.path.insert(0, str(MODULE_DIR))

import route_policy


class RoutePolicyConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = route_policy.load_policy_config(PROJECT_DIR / "tests" / "fixtures" / "route-policies.fixed.json")
        cls.policies = {item["region"]: item for item in cls.config["policies"]}

    def test_single_config_drives_shadow_policies_and_safe_mihomo_groups(self):
        self.assertEqual(self.config["schema_version"], 1)
        self.assertEqual(self.config["mode"], "shadow")
        self.assertEqual(set(self.policies), {"TW", "HK"})
        for policy in self.policies.values():
            discovery = route_policy.discovery_group(policy)
            self.assertTrue(discovery["include-all-proxies"])
            self.assertTrue(discovery["hidden"])
            self.assertEqual(discovery["empty-fallback"], "REJECT")
            self.assertIn("direct", discovery["exclude-type"])
            self.assertNotIn("(?=", discovery["filter"])
            self.assertNotIn("(?!", discovery["filter"])

    def test_current_static_candidates_match_their_policy_filters(self):
        self.assertEqual(len(self.policies["TW"]["static_candidates"]), 11)
        self.assertEqual(len(self.policies["HK"]["static_candidates"]), 5)
        for policy in self.policies.values():
            for name in policy["static_candidates"]:
                with self.subTest(policy=policy["id"], name=name):
                    self.assertTrue(route_policy.name_matches(policy, name))

    def test_information_and_non_residential_nodes_are_excluded(self):
        rejected = {
            "TW": ["台湾高速01", "台灣到期 2026-10-01", "🇹🇼 剩余流量 100GB", "台湾官网"],
            "HK": ["香港普通节点", "Hong Kong traffic reset", "🇭🇰 套餐公告", "香港客服"],
        }
        for region, names in rejected.items():
            for name in names:
                with self.subTest(region=region, name=name):
                    self.assertFalse(route_policy.name_matches(self.policies[region], name))

    def test_multiplier_and_protocol_labels_do_not_exclude_residential_nodes(self):
        accepted = {
            "TW": ["3x 台湾 HINET 家宽 HY2", "VLESS 台灣 Seednet 動態家寬", "CF 🇹🇼 家寬 10x"],
            "HK": ["10x Hong Kong HKT 家宽 VLESS", "CF 香港動態家寬 HY2", "🇭🇰 HKBN 家寬 3x"],
        }
        for region, names in accepted.items():
            for name in names:
                with self.subTest(region=region, name=name):
                    self.assertTrue(route_policy.name_matches(self.policies[region], name))

    def test_staged_mihomo_config_is_self_contained_and_fail_closed(self):
        rendered = route_policy.render_staged_mihomo_config(self.config)
        self.assertIn("proxy-groups:", rendered)
        self.assertIn("proxies:", rendered)
        self.assertEqual(rendered.count("empty-fallback: REJECT"), 2)
        self.assertNotIn("COMPATIBLE", rendered)
        self.assertIn("MATCH,REJECT", rendered)

    def test_active_mode_requires_separate_approval_and_rss_budget(self):
        active = copy.deepcopy(self.config)
        active["mode"] = "active"
        with self.assertRaises(route_policy.PolicyConfigError):
            route_policy.validate_policy_config(active)
        active["activation"] = {
            "approved": True, "max_rss_delta_mb": 1.0, "measured_rss_delta_mb": 1.1,
        }
        with self.assertRaises(route_policy.PolicyConfigError):
            route_policy.validate_policy_config(active)
        active["activation"]["measured_rss_delta_mb"] = 0.8
        route_policy.validate_policy_config(active)
        generated = route_policy.active_group(active["policies"][0], mode="active")
        self.assertTrue(generated["include-all-proxies"])
        self.assertEqual(generated["empty-fallback"], "REJECT")
        self.assertFalse(generated["hidden"], "the traffic-carrying active group must remain visible")

    def test_policy_string_lists_reject_non_string_elements(self):
        for field, bad_value in (
            ("exclude_types", ["direct", 7]),
            ("business_test_urls", ["https://example.invalid/", None]),
            ("static_candidates", ["台湾 HINET 家宽", {"name": "not-a-string"}]),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(self.config)
                invalid["policies"][0][field] = bad_value
                with self.assertRaises(route_policy.PolicyConfigError):
                    route_policy.validate_policy_config(invalid)


if __name__ == "__main__":
    unittest.main()
