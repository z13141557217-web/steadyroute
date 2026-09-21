#!/usr/bin/python3
"""Serve a synthetic, controller-free candidate acceptance preview."""

import argparse
import pathlib
import sys
import time


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src" / "steadyroute"))

import weighted_router as router


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    now = int(time.time())
    state = router.state_contract.new_state()
    proxy_data = {}
    for policy in router.POLICIES:
        names = policy["static_candidates"]
        proxy_data[policy["group_name"]] = {"now": names[0], "all": names}
        proxy_data[policy["discovery_group_name"]] = {"now": names[0], "all": names}
    router.candidate_registry.reconcile(router.POLICY_CONFIG, state, proxy_data, now - 20)
    router.candidate_registry.reconcile(router.POLICY_CONFIG, state, proxy_data, now)
    for policy in router.POLICIES:
        for index, name in enumerate(policy["static_candidates"][:3]):
            state["nodes"][name] = {
                "samples": 10 if index == 0 else 4 + index,
                "success_streak": 3 if index == 0 else index,
                "last_success": True,
                "business_successes": 1 if index == 0 else 0,
                "score": 80 + index * 20,
            }
    first_group = router.POLICIES[0]["group_name"]
    state["groups"].setdefault(first_group, {}).update({
        "last_seen": proxy_data[first_group]["now"],
        "manual_hold_until": now + 125,
    })
    router.candidate_registry.refresh_lifecycles(router.POLICY_CONFIG, state, now)
    state.update({"updated_at": now, "controller_connected": True})
    router.update_dashboard_cache(router.build_status_snapshots(state, proxy_data, [], now=now))
    router.DASHBOARD_PORT = args.port
    server = router.http.server.HTTPServer((router.DASHBOARD_HOST, args.port), router.DashboardHandler)
    print("http://127.0.0.1:%d/" % args.port, flush=True)
    print("http://127.0.0.1:%d/candidate-acceptance" % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
