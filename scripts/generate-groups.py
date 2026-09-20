#!/usr/bin/python3
"""Generate or verify the Clash Verge enhancement from route policies."""

import argparse
import pathlib
import sys


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src" / "steadyroute"))

import route_policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = route_policy.load_policy_config(PROJECT_DIR / "config" / "route-policies.json")
    rendered = route_policy.render_enhancement_yaml(config)
    target = PROJECT_DIR / "config" / "clash-verge" / "groups.yaml"
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != rendered:
            print("groups.yaml differs from config/route-policies.json", file=sys.stderr)
            return 1
        return 0
    target.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
