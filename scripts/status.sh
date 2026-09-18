#!/bin/sh
set -eu

LIVE_DIR='/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router'
LOG_DIR='/Users/nurture/Library/Logs/Clash-Verge-Stability-Router'
LABEL='com.nurture.clash-stability-router'

echo 'Service'
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | rg 'state =|pid =|runs =|last exit code' || true

echo 'Endpoint'
curl --noproxy '*' -sS --max-time 3 -o /dev/null -w 'HTTP %{http_code} in %{time_total}s\n' http://127.0.0.1:17654/api/status || true

echo 'Runtime status'
python3 "$LIVE_DIR/weighted_router.py" --status 2>/dev/null | sed -n '1,80p' || true

echo 'Logs'
du -h "$LOG_DIR"/* 2>/dev/null || true

