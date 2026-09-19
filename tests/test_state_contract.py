import json
import pathlib
import tempfile
import unittest


PROJECT_DIR = pathlib.Path(__file__).parents[1]
FIXTURE_PATH = PROJECT_DIR / "src" / "steadyroute" / "fixtures" / "status_contract_v2.json"

import sys
sys.path.insert(0, str(PROJECT_DIR / "src" / "steadyroute"))
import state_contract as contract


class DecisionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_every_fixed_group_scenario_has_expected_backend_decision(self):
        for case in self.fixture["group_scenarios"]:
            with self.subTest(case=case["scenario"]):
                decision = contract.resolve_group_decision(case["facts"], self.fixture["generated_at"])
                self.assertEqual(decision["code"], case["expected"]["code"])
                self.assertEqual(decision["title"], case["expected"]["title"])
                self.assertIn("reason_code", decision)
                self.assertIn("next_action", decision)

    def test_every_fixed_node_scenario_has_expected_lifecycle(self):
        for case in self.fixture["node_scenarios"]:
            with self.subTest(case=case["scenario"]):
                lifecycle = contract.resolve_node_lifecycle(case["facts"], self.fixture["generated_at"])
                self.assertEqual(lifecycle["code"], case["expected"]["lifecycle"])
                self.assertEqual(lifecycle["title"], case["expected"]["title"])

    def test_all_codes_have_unique_stable_chinese_copy(self):
        self.assertEqual(
            set(contract.GROUP_DECISION_COPY),
            {"stable", "candidate_confirming", "handover_pending", "handover_grace",
             "recovery_observing", "cooldown", "manual_hold", "degraded",
             "failover_now", "no_candidate", "controller_offline"},
        )
        self.assertEqual(
            set(contract.NODE_LIFECYCLE_COPY),
            {"discovered", "warming", "healthy", "degraded", "quarantined", "half_open", "retired"},
        )
        for mapping in (contract.GROUP_DECISION_COPY, contract.NODE_LIFECYCLE_COPY):
            titles = [item["title"] for item in mapping.values()]
            descriptions = [item["description"] for item in mapping.values()]
            next_actions = [item["next_action"] for item in mapping.values()]
            self.assertEqual(len(titles), len(set(titles)))
            self.assertEqual(len(descriptions), len(set(descriptions)))
            self.assertEqual(len(next_actions), len(set(next_actions)))

    def test_transition_event_is_emitted_once_per_real_change(self):
        state = contract.new_state()
        first = contract.record_transition(
            state, "group", "group-tw", "stable", "degraded", "quality_drop", 1789762600
        )
        duplicate = contract.record_transition(
            state, "group", "group-tw", "degraded", "degraded", "quality_drop", 1789762601
        )
        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertEqual(len(state["events"]), 1)
        self.assertEqual(state["events"][0]["from_state"], "stable")
        self.assertEqual(state["events"][0]["to_state"], "degraded")

    def test_declared_transition_paths_are_valid(self):
        transitions = self.fixture["transitions"]
        for key in ("group_normal", "group_failure"):
            path = transitions[key]
            for old_state, new_state in zip(path, path[1:]):
                with self.subTest(path=key, transition=(old_state, new_state)):
                    self.assertTrue(contract.transition_allowed("group", old_state, new_state))
        node_path = transitions["node"]
        for old_state, new_state in zip(node_path, node_path[1:]):
            self.assertTrue(contract.transition_allowed("node", old_state, new_state))
        for exception in transitions["group_exception_branches"]:
            self.assertTrue(contract.transition_allowed("group", "stable", exception))
        self.assertTrue(contract.transition_allowed("node", "healthy", "retired"))
        self.assertFalse(contract.transition_allowed("node", "retired", "healthy"))

    def test_undeclared_group_and_node_transitions_are_rejected(self):
        state = contract.new_state()
        with self.assertRaises(contract.InvalidTransitionError):
            contract.record_transition(
                state, "group", "group-tw", "stable", "handover_grace",
                "skipped_confirmation", 1789762600,
            )
        with self.assertRaises(contract.InvalidTransitionError):
            contract.record_transition(
                state, "node", "node-retired", "retired", "healthy",
                "history_must_not_revive", 1789762600,
            )
        with self.assertRaises(contract.InvalidTransitionError):
            contract.record_transition(
                state, "group", "group-tw", "unknown", "manual_hold",
                "unknown_state", 1789762600,
            )
        with self.assertRaises(contract.InvalidTransitionError):
            contract.record_transition(
                state, "node", "node-unknown", "unknown", "unknown",
                "unknown_state", 1789762600,
            )
        self.assertEqual(state["events"], [])

    def test_declared_intermediate_path_records_ordered_events(self):
        state = contract.new_state()
        contract.record_transition_path(
            state, "group", "group-tw", "candidate_confirming", "handover_grace",
            [
                ("handover_pending", "confirmation_complete"),
                ("handover_grace", "healthy_old_connections_present"),
            ],
            1789762600,
        )
        self.assertEqual(
            [(event["from_state"], event["to_state"], event["reason_code"]) for event in state["events"]],
            [
                ("candidate_confirming", "handover_pending", "confirmation_complete"),
                ("handover_pending", "handover_grace", "healthy_old_connections_present"),
            ],
        )

    def test_node_ui_id_is_stable_non_reversible_identifier(self):
        first = contract.node_ui_id("AI 台湾家宽线路", "台湾 HINET 家宽02")
        self.assertEqual(first, contract.node_ui_id("AI 台湾家宽线路", "台湾 HINET 家宽02"))
        self.assertNotEqual(first, contract.node_ui_id("AI 台湾家宽线路", "台湾 HINET 家宽03"))
        self.assertNotIn("HINET", first)


class PersistentStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temporary.name) / "state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_v1_migrates_deterministically_and_keeps_atomic_backup(self):
        original = {"version": 1, "nodes": {"node": {"samples": 3}}, "groups": {}, "updated_at": 7}
        encoded = json.dumps(original, sort_keys=True).encode("utf-8")
        self.path.write_bytes(encoded)
        migrated = contract.load_persistent_state(self.path, now=1789762600)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["nodes"], original["nodes"])
        self.assertNotIn("version", migrated)
        self.assertEqual((self.path.parent / "state.v1-backup.json").read_bytes(), encoded)
        self.assertEqual(migrated, contract.migrate_state(original))

    def test_corrupt_state_is_preserved_and_rebuilt_safely(self):
        self.path.write_text("{broken", encoding="utf-8")
        state = contract.load_persistent_state(self.path, now=1789762600)
        self.assertEqual(state, contract.new_state())
        self.assertEqual(
            (self.path.parent / "state.corrupt-1789762600.json").read_text(encoding="utf-8"),
            "{broken",
        )

    def test_failed_migration_preserves_old_file_and_returns_safe_state(self):
        self.path.write_text('{"version": 1, "nodes": [], "groups": {}}', encoding="utf-8")
        state = contract.load_persistent_state(self.path, now=1789762600)
        self.assertEqual(state, contract.new_state())
        self.assertTrue((self.path.parent / "state.migration-failed-1789762600.json").exists())

    def test_unknown_future_version_is_never_overwritten(self):
        original = '{"schema_version": 99, "future": true}'
        self.path.write_text(original, encoding="utf-8")
        with self.assertRaises(contract.FutureStateVersionError):
            contract.load_persistent_state(self.path, now=1789762600)
        with self.assertRaises(contract.FutureStateVersionError):
            contract.save_persistent_state(self.path, {"schema_version": 99})
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_save_is_atomic_and_round_trips_v2(self):
        state = contract.new_state()
        state["updated_at"] = 1789762600
        contract.save_persistent_state(self.path, state)
        self.assertEqual(contract.load_persistent_state(self.path), state)
        self.assertEqual(list(self.path.parent.glob("state-*.json")), [])


if __name__ == "__main__":
    unittest.main()
