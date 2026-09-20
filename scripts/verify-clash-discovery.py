#!/usr/bin/python3
"""Read-only verification that configured discovery groups exist in /proxies."""

import argparse
import json
import pathlib
import sys


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src" / "steadyroute"))

import clash_group_deploy
import route_policy


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, default=PROJECT_DIR / "config" / "route-policies.json")
    args = parser.parse_args(argv)
    try:
        config = route_policy.load_policy_config(args.policy)
        payload = clash_group_deploy.read_controller_proxies(args.socket)
        result = clash_group_deploy.verify_discovery_payload(config, payload)
    except (clash_group_deploy.GroupEnhancementError, route_policy.PolicyConfigError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
