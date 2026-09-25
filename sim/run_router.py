#!/usr/bin/env python3
"""Run one SteadyRoute source tree as a daemon against a fake controller.

Usage: run_router.py SOURCE_DIR BASE_DIR SOCKET PORT

Production paths are redirected into BASE_DIR before main() starts, so the
real ~/Library locations are never touched.
"""

import importlib.util
import os
import pathlib
import sys


def main():
    source, base_dir, socket_path, port = sys.argv[1:5]
    module_dir = pathlib.Path(source).resolve() / "src" / "steadyroute"
    sys.path.insert(0, str(module_dir))
    os.environ["STEADYROUTE_CONTROLLER_SOCKET"] = socket_path
    spec = importlib.util.spec_from_file_location("weighted_router", str(module_dir / "weighted_router.py"))
    router = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router)
    router.BASE_DIR = base_dir
    router.STATE_PATH = os.path.join(base_dir, "state.json")
    router.LOCK_PATH = os.path.join(base_dir, "router.lock")
    router.DASHBOARD_PATH = str(module_dir / "dashboard.html")
    router.DASHBOARD_PORT = int(port)
    if hasattr(router, "LOG_DIR"):
        router.LOG_DIR = base_dir
    sys.argv = ["weighted_router.py", "--daemon"]
    return router.main()


if __name__ == "__main__":
    sys.exit(main())
