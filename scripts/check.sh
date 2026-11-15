#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Tests must never inherit the installed app's state, logs or controller socket.
STEADYROUTE_CHECK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/steadyroute-check.XXXXXX")
trap 'rm -rf "$STEADYROUTE_CHECK_DIR"' EXIT HUP INT TERM
export STEADYROUTE_BASE_DIR="$STEADYROUTE_CHECK_DIR/app"
export STEADYROUTE_LOG_DIR="$STEADYROUTE_CHECK_DIR/logs"
export STEADYROUTE_CONTROLLER_SOCKET="$STEADYROUTE_CHECK_DIR/offline.sock"
mkdir -p "$STEADYROUTE_BASE_DIR" "$STEADYROUTE_LOG_DIR"

# Byte-compile every Python file we ship or run (a hand-written list kept missing scripts).
python3 -m compileall -q "$PROJECT_DIR/src" "$PROJECT_DIR/scripts" "$PROJECT_DIR/sim" "$PROJECT_DIR/tests"
python3 -m unittest discover -s "$PROJECT_DIR/tests" -p 'test_*.py' -v


# Standard library only, so a missing tool can never turn this into a silent pass.
python3 "$PROJECT_DIR/scripts/leak-scan.py" "$PROJECT_DIR"

echo "SteadyRoute checks passed."
