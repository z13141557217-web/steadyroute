#!/usr/bin/env python3
"""Serve the candidate dashboard and bundled notes without touching Mihomo."""

import argparse
import importlib.util
import json
import pathlib
import sys
import tempfile
import time


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_DIR / "src" / "steadyroute"
sys.path.insert(0, str(SOURCE_DIR))
import weighted_router as router

BUILDER_SPEC = importlib.util.spec_from_file_location(
    "release_notes_builder", str(PROJECT_DIR / "scripts" / "build-release-notes.py")
)
builder = importlib.util.module_from_spec(BUILDER_SPEC)
BUILDER_SPEC.loader.exec_module(builder)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--status-mode", choices=("simulated", "stale", "offline"), default="simulated")
    parser.add_argument("--notes-mode", choices=("present", "missing"), default="present")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="steadyroute-v0.5-preview-") as directory:
        notes_path = pathlib.Path(directory) / "release_notes.json"
        notes_path.write_text(json.dumps(builder.parse_release_history(), ensure_ascii=False), encoding="utf-8")
        router.DASHBOARD_PATH = str(SOURCE_DIR / "dashboard.html")
        router.RELEASE_NOTES_HTML_PATH = str(SOURCE_DIR / "release_notes.html")
        router.RELEASE_NOTES_PATH = str(notes_path if args.notes_mode == "present" else pathlib.Path(directory) / "missing.json")
        now = int(time.time())
        state = router.state_contract.new_state()
        proxy_data = {}
        for policy in router.POLICIES:
            names = policy["static_candidates"]
            proxy_data[policy["group_name"]] = {"now": names[0], "all": names}
            state["nodes"][names[0]] = {
                "samples": 12, "success_streak": 12, "last_success": True,
                "availability_ewma": 1.0, "latency_ewma": 85.0, "jitter_ewma": 4.0,
                "score": 100.0, "short_results": [1] * 12,
            }
        state.update({"updated_at": now, "controller_connected": True})
        snapshots = router.build_status_snapshots(state, proxy_data, [], now=now)
        snapshots["v1"]["service"]["preview_mode"] = True
        snapshots["legacy"]["service"]["preview_mode"] = True
        if args.status_mode == "stale":
            snapshots["v1"]["service"]["state_stale"] = True
        router.update_dashboard_cache(snapshots)
        if args.status_mode == "offline":
            original_cached_response = router.cached_api_response
            def offline_status(path):
                if path in ("/api/status", "/api/v1/status"):
                    return 503, "application/json; charset=utf-8", b'{"error":"preview_offline"}'
                return original_cached_response(path)
            router.cached_api_response = offline_status
        server = router.DashboardServer((router.DASHBOARD_HOST, args.port), router.DashboardHandler)
        print("候选看板（模拟状态，无 Mihomo 请求）：http://127.0.0.1:%d/" % args.port, flush=True)
        print("只读更新日志：http://127.0.0.1:%d/release-notes" % args.port, flush=True)
        print("模拟状态：%s；版本记录：%s" % (args.status_mode, args.notes_mode), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
