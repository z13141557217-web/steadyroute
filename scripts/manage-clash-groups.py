#!/usr/bin/python3
"""Dry-run-first deployment and rollback for Clash Verge group enhancement."""

import argparse
import json
import pathlib
import sys


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src" / "steadyroute"))

import clash_group_deploy


def build_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("deploy", "rollback"):
        item = subparsers.add_parser(command)
        item.add_argument("--profiles-yaml", type=pathlib.Path, required=True)
        item.add_argument("--profile-dir", type=pathlib.Path, required=True)
        item.add_argument("--core", type=pathlib.Path, required=True)
        item.add_argument("--policy", type=pathlib.Path, default=PROJECT_DIR / "config" / "route-policies.json")
        item.add_argument(
            "--generated-groups", type=pathlib.Path,
            default=PROJECT_DIR / "config" / "clash-verge" / "groups.yaml",
        )
        item.add_argument("--apply", action="store_true")
        if command == "rollback":
            item.add_argument("--backup", type=pathlib.Path, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        manager = clash_group_deploy.ClashGroupEnhancementManager(
            profiles_yaml=args.profiles_yaml,
            profile_dir=args.profile_dir,
            policy_path=args.policy,
            generated_groups_path=args.generated_groups,
            staged_validator=clash_group_deploy.mihomo_staged_validator(args.core),
        )
        if args.command == "deploy":
            result = manager.apply(apply=args.apply)
        else:
            result = manager.rollback(args.backup, apply=args.apply)
    except (clash_group_deploy.GroupEnhancementError, OSError, ValueError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
