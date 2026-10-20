import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("flow_router")


def healthy(score):
    return {
        "last_success": True,
        "samples": 20,
        "success_streak": 10,
        "failure_streak": 0,
        "effective_failure_streak": 0,
        "availability_ewma": 1.0,
        "latency_ewma": float(score),
        "jitter_ewma": 0.0,
        "score": float(score),
        "short_results": [1] * 20,
        "long_buckets": [{"hour": 1, "success": 20, "total": 20, "latency_sum": score * 20}],
    }


class DecisionEventFlowTests(unittest.TestCase):
    def setUp(self):
        self.group = "AI 台湾家宽线路"
        self.current, self.target = router.GROUPS[self.group][:2]
        self.candidates = [self.current, self.target]
        self.proxy = {self.group: {"now": self.current}}
        self.state = router.state_contract.new_state()
        self.state.update({
            "controller_connected": True,
            "updated_at": 1789762600,
            "nodes": {self.current: healthy(500), self.target: healthy(100)},
            "groups": {self.group: {"last_seen": self.current, "last_switch_at": 0}},
        })
        router.build_status_snapshots(self.state, self.proxy, [], now=1789762600, memory_mb=24.1)

    def evaluate_and_snapshot(self, now, connections):
        with mock.patch.object(router.time, "time", return_value=now), \
                mock.patch.object(router, "business_preflight", return_value=True), \
                mock.patch.object(router, "select_node") as select_node, \
                mock.patch.object(router, "close_old_connections") as close_connections:
            router.evaluate_group(
                self.group, self.candidates, self.proxy, connections, self.state, False
            )
            snapshots = router.build_status_snapshots(
                self.state, self.proxy, connections, now=now, memory_mb=24.1
            )
        group = next(item for item in snapshots["v1"]["groups"] if item["name"] == self.group)
        return group, select_node.call_args_list, close_connections.call_args_list

    def confirm_all_but_last(self):
        """A minute of 5-second cycles that all find the target better, minus the last one."""
        required = router.PERFORMANCE_CONFIRMATIONS
        for index in range(1, required):
            group, selected, _ = self.evaluate_and_snapshot(1789762600 + 5 * index, [])
            self.assertEqual(group["decision"]["code"], "candidate_confirming")
            self.assertEqual(group["confirmation"], {"current": index, "required": required})
            self.assertEqual(selected, [])
        return 1789762600 + 5 * required

    def test_three_confirmations_switch_immediately_and_emit_ordered_handover_events(self):
        self.assertEqual(router.PERFORMANCE_CONFIRMATIONS * router.PROBE_INTERVAL_SECONDS, 60)
        last = self.confirm_all_but_last()
        old_connections = [{"id": "old", "chains": [self.current, self.group]}]
        group, selected, closed = self.evaluate_and_snapshot(last, old_connections)
        self.assertEqual(selected[0].args, (self.group, self.target, False))
        self.assertEqual(closed, [], "performance recovery must preserve healthy old connections")
        self.assertEqual(group["decision"]["code"], "handover_grace")
        self.assertEqual(
            [
                (event["from_state"], event["to_state"], event["reason_code"])
                for event in self.state["events"][-2:]
            ],
            [
                ("candidate_confirming", "handover_pending", "confirmation_complete"),
                ("handover_pending", "handover_grace", "healthy_old_connections_present"),
            ],
        )

    def test_no_old_connections_emits_zero_duration_grace_then_observes_recovery(self):
        last = self.confirm_all_but_last()
        group, selected, _ = self.evaluate_and_snapshot(last, [])
        self.assertEqual(selected[0].args, (self.group, self.target, False))
        self.assertEqual(group["decision"]["code"], "recovery_observing")
        self.assertEqual(
            [
                (event["from_state"], event["to_state"], event["reason_code"])
                for event in self.state["events"][-3:]
            ],
            [
                ("candidate_confirming", "handover_pending", "confirmation_complete"),
                ("handover_pending", "handover_grace", "no_old_connections"),
                ("handover_grace", "recovery_observing", "post_switch_validation"),
            ],
        )

        switched_proxy = {self.group: {"now": self.target}}
        snapshots = router.build_status_snapshots(
            self.state, switched_proxy, [], now=1789763021, memory_mb=24.1
        )
        group = next(item for item in snapshots["v1"]["groups"] if item["name"] == self.group)
        self.assertEqual(group["decision"]["code"], "cooldown")
        snapshots = router.build_status_snapshots(
            self.state, switched_proxy, [], now=1789764461, memory_mb=24.1
        )
        group = next(item for item in snapshots["v1"]["groups"] if item["name"] == self.group)
        self.assertEqual(group["decision"]["code"], "stable")
        self.assertEqual(
            [(event["from_state"], event["to_state"]) for event in self.state["events"][-2:]],
            [("recovery_observing", "cooldown"), ("cooldown", "stable")],
        )


if __name__ == "__main__":
    unittest.main()
