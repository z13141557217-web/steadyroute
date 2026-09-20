#!/usr/bin/python3
"""Validate generated dynamic groups with the installed Mihomo core."""

import argparse
import pathlib
import subprocess
import sys
import tempfile


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src" / "steadyroute"))

import route_policy


DEFAULT_CORE = pathlib.Path("/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", type=pathlib.Path, default=DEFAULT_CORE)
    args = parser.parse_args()
    if not args.core.is_file():
        print("Mihomo core not found: %s" % args.core, file=sys.stderr)
        return 2
    config = route_policy.load_policy_config(PROJECT_DIR / "config" / "route-policies.json")
    rendered = route_policy.render_staged_mihomo_config(config)
    with tempfile.TemporaryDirectory(prefix="steadyroute-mihomo-") as directory:
        path = pathlib.Path(directory) / "staged.yaml"
        path.write_text(rendered, encoding="utf-8")
        result = subprocess.run(
            [str(args.core), "-t", "-f", str(path)],
            cwd=directory,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.stdout:
        print(result.stdout.rstrip())
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
