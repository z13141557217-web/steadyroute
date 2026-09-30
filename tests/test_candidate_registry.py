import copy
import pathlib
import sys
import tempfile
import unittest


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
sys.path.insert(0, str(MODULE_DIR))

import candidate_registry
import route_policy
import state_contract


class CandidateRegistryTests(unittest.TestCase):
    def setUp(self):
        self.config = route_policy.load_policy_config(PROJECT_DIR / "tests" / "fixtures" / "route-policies.fixed.json")
        self.policy = self.config["policies"][0]
        self.discovery = self.policy["discovery_group_name"]
        self.state = state_contract.new_state()
        self.now = 1_800_000_000

    def snapshot(self, names, current=None):
        data = {self.discovery: {"all": list(names), "now": names[0] if names else "REJECT"}}
        if current is not None:
            data[self.policy["group_name"]] = {"all": list(names), "now": current}
        return data

    def confirm(self, names, start=None):
        start = self.now if start is None else start
        first = candidate_registry.reconcile(self.config, self.state, self.snapshot(names), start)
        second = candidate_registry.reconcile(self.config, self.state, self.snapshot(names), start + 20)
        return first, second

    def policy_state(self):
        return self.state["candidate_registry"]["policies"][self.policy["id"]]

    def test_difference_requires_two_consecutive_successful_snapshots(self):
        names = self.policy["static_candidates"][:2]
        first = candidate_registry.reconcile(self.config, self.state, self.snapshot(names), self.now)
        self.assertEqual(first["generation"], 0)
        self.assertEqual(self.policy_state()["candidates"], [])
        self.assertEqual(self.policy_state()["proposed_candidates"], names)

        second = candidate_registry.reconcile(self.config, self.state, self.snapshot(names), self.now + 20)
        self.assertEqual(second["generation"], 1)
        self.assertEqual(self.policy_state()["candidates"], names)
        self.assertEqual({item["lifecycle"] for item in self.policy_state()["nodes"].values()}, {"discovered"})

    def test_transient_empty_offline_missing_and_malformed_snapshots_never_remove_all(self):
        names = self.policy["static_candidates"][:3]
        self.confirm(names)
        generation = self.state["subscription"]["generation"]
        candidate_registry.reconcile(self.config, self.state, self.snapshot([]), self.now + 40)
        candidate_registry.reconcile(self.config, self.state, {}, self.now + 60, controller_ok=False)
        candidate_registry.reconcile(self.config, self.state, {}, self.now + 80)
        candidate_registry.reconcile(
            self.config, self.state, {self.discovery: {"all": "not-an-array"}}, self.now + 100
        )
        self.assertEqual(self.policy_state()["candidates"], names)
        self.assertEqual(self.state["subscription"]["generation"], generation)
        self.assertEqual(self.policy_state()["group_status"], "malformed")

    def test_confirmed_add_remove_rename_and_delete_all_have_safe_lifecycles(self):
        old = self.policy["static_candidates"][:2]
        self.confirm(old)
        added = old + [self.policy["static_candidates"][2]]
        self.confirm(added, self.now + 40)
        self.assertEqual(self.state["subscription"]["generation"], 2)

        renamed = "台湾 HINET 家宽 新名称"
        replacement = [renamed, added[1], added[2]]
        self.confirm(replacement, self.now + 80)
        policy_state = self.policy_state()
        self.assertEqual(policy_state["nodes"][old[0]]["lifecycle"], "retired")
        self.assertEqual(policy_state["nodes"][renamed]["lifecycle"], "discovered")
        self.assertEqual(policy_state["nodes"][renamed]["suspected_rename_from"], old[0])

        self.confirm([], self.now + 120)
        self.assertEqual(self.policy_state()["candidates"], [])
        self.assertEqual(self.policy_state()["last_nonempty_candidates"], replacement)
        self.assertTrue(all(item["lifecycle"] == "retired" for item in self.policy_state()["nodes"].values()))
        codes = [item["code"] for item in self.state["events"]]
        self.assertIn("NO_CANDIDATE", codes)
        self.assertNotIn("DIRECT", str(self.state))

    def test_builtin_policies_are_never_candidates(self):
        names = ["DIRECT", "COMPATIBLE", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE"]
        valid = ["台湾 HINET 家宽 A"]
        self.confirm(names + valid)
        self.assertEqual(self.policy_state()["candidates"], valid)

    def test_warmup_requires_samples_streak_business_success_and_no_quarantine(self):
        name = "台湾 HINET 家宽 A"
        self.confirm([name])
        health = self.state["nodes"].setdefault(name, {})
        health.update({"samples": 10, "success_streak": 3, "last_success": True})
        candidate_registry.refresh_lifecycles(self.config, self.state, self.now + 40)
        self.assertEqual(self.policy_state()["nodes"][name]["lifecycle"], "warming")

        health["business_last_success"] = True
        candidate_registry.refresh_lifecycles(self.config, self.state, self.now + 60)
        self.assertEqual(self.policy_state()["nodes"][name]["lifecycle"], "healthy")

        health["quarantine_until"] = self.now + 1000
        candidate_registry.refresh_lifecycles(self.config, self.state, self.now + 80)
        self.assertEqual(self.policy_state()["nodes"][name]["lifecycle"], "quarantined")

    def test_retired_records_survive_restart_then_purge_after_24_hours(self):
        name = "台湾 HINET 家宽 A"
        self.confirm([name])
        self.confirm([], self.now + 40)
        restored = copy.deepcopy(self.state)
        candidate_registry.purge_retired(self.config, restored, self.now + 40 + 86399)
        node_map = restored["candidate_registry"]["policies"][self.policy["id"]]["nodes"]
        self.assertIn(name, node_map)
        candidate_registry.purge_retired(self.config, restored, self.now + 40 + 86420)
        self.assertNotIn(name, node_map)
        self.assertEqual(restored["events"][-1]["code"], "NODE_RETIREMENT_PURGED")

    def test_registry_round_trips_through_the_versioned_state_store(self):
        names = self.policy["static_candidates"][:2]
        self.confirm(names)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            state_contract.save_persistent_state(path, self.state)
            restored = state_contract.load_persistent_state(path)
        self.assertEqual(
            restored["candidate_registry"]["policies"][self.policy["id"]]["candidates"], names
        )
        self.assertEqual(restored["subscription"]["generation"], 1)

    def test_current_node_disappearance_only_proposes_mature_same_policy_backup(self):
        current, backup, warming = ["台湾 HINET 家宽 %s" % value for value in ("A", "B", "C")]
        self.confirm([current, backup, warming])
        for name in (current, backup):
            self.state["nodes"][name] = {
                "samples": 10, "success_streak": 3, "last_success": True,
                "business_last_success": True, "score": 100 if name == backup else 200,
            }
        self.state["nodes"][warming] = {"samples": 9, "success_streak": 9, "last_success": True}
        candidate_registry.refresh_lifecycles(self.config, self.state, self.now + 40)
        self.confirm([backup, warming], self.now + 60)
        plan = candidate_registry.current_node_plan(self.policy, self.state, current)
        self.assertEqual(plan, {"status": "current_removed", "target": backup, "execute": False})

        self.confirm([warming], self.now + 100)
        plan = candidate_registry.current_node_plan(self.policy, self.state, backup)
        self.assertEqual(plan, {"status": "no_candidate", "target": None, "execute": False})

    def test_generation_does_not_change_for_health_or_identical_snapshots(self):
        names = self.policy["static_candidates"][:2]
        self.confirm(names)
        generation = self.state["subscription"]["generation"]
        self.state["nodes"][names[0]] = {"samples": 10, "success_streak": 3, "last_success": True}
        candidate_registry.refresh_lifecycles(self.config, self.state, self.now + 40)
        candidate_registry.reconcile(self.config, self.state, self.snapshot(names), self.now + 60)
        self.assertEqual(self.state["subscription"]["generation"], generation)

    def test_registry_handles_16_50_and_100_nodes_with_bounded_events(self):
        for count in (16, 50, 100):
            with self.subTest(count=count):
                state = state_contract.new_state()
                names = ["台湾 HINET 家宽 %03d" % index for index in range(count)]
                proxy = self.snapshot(names)
                candidate_registry.reconcile(self.config, state, proxy, self.now)
                candidate_registry.reconcile(self.config, state, proxy, self.now + 20)
                policy_state = state["candidate_registry"]["policies"][self.policy["id"]]
                self.assertEqual(len(policy_state["candidates"]), count)
                self.assertLessEqual(len(state["events"]), state_contract.EVENT_LIMIT)


if __name__ == "__main__":
    unittest.main()
