#!/usr/bin/python3
"""Measure cached status endpoints in an isolated source tree."""

import argparse
import importlib.util
import json
import pathlib
import resource
import sys
import threading
import time
import urllib.request


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    args = parser.parse_args()
    module_dir = args.source.resolve() / "src" / "steadyroute"
    sys.path.insert(0, str(module_dir))
    spec = importlib.util.spec_from_file_location("benchmark_router", str(module_dir / "weighted_router.py"))
    router = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router)
    state = router.state_contract.new_state()
    now = int(time.time())
    state.update({"updated_at": now, "controller_connected": False})
    snapshots = router.build_status_snapshots(state, {}, [], now=now, memory_mb=0)
    router.update_dashboard_cache(snapshots)
    server = router.http.server.HTTPServer(("127.0.0.1", 0), router.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    endpoints = ["/api/status"]
    if router.cached_api_response("/api/v1/status")[0] == 200:
        endpoints.append("/api/v1/status")
    results = {}
    for endpoint in endpoints:
        timings = []
        started = time.perf_counter()
        with urllib.request.urlopen(base + endpoint, timeout=2) as response:
            body = response.read()
        single = (time.perf_counter() - started) * 1000
        for _ in range(100):
            started = time.perf_counter()
            with urllib.request.urlopen(base + endpoint, timeout=2) as response:
                response.read()
            timings.append((time.perf_counter() - started) * 1000)
        results[endpoint] = {
            "single_ms": round(single, 3),
            "hundred_total_ms": round(sum(timings), 3),
            "hundred_mean_ms": round(sum(timings) / 100, 3),
            "hundred_p95_ms": round(percentile(timings, 0.95), 3),
            "response_bytes": len(body),
        }
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    time.sleep(1)
    idle_cpu = (time.process_time() - cpu_start) / (time.perf_counter() - wall_start) * 100
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = usage / 1024 / 1024 if sys.platform == "darwin" else usage / 1024
    server.shutdown()
    print(json.dumps({
        "rss_mb": round(rss_mb, 3),
        "idle_cpu_percent": round(idle_cpu, 4),
        "state_bytes": len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "endpoints": results,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
