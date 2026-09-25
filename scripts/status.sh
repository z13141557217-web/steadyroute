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

echo 'Logs (bounded since v0.4.4: router 20 MiB, events 10 MiB, errors 3 MiB at most)'
du -h "$LOG_DIR"/* 2>/dev/null || true
printf 'Total: '
du -sh "$LOG_DIR" 2>/dev/null | cut -f1 || echo '?'
echo 'Recent decisions (events.jsonl)'
tail -n 5 "$LOG_DIR/events.jsonl" 2>/dev/null || echo '(none yet)'
echo 'Recent errors (router-error.log)'
tail -n 5 "$LOG_DIR/router-error.log" 2>/dev/null || echo '(none)'

