#!/usr/bin/env python3
"""稳航分享版安装器（macOS · Clash Verge）。

    python3 installer.py install     安装或升级，已有的状态和配置会保留
    python3 installer.py uninstall   停止并删除（日志保留，加 --purge 一起删）
    python3 installer.py status      看看有没有在运行

Standard library only (Python 3.9, the version macOS ships with the command line tools).
The installer never touches Clash Verge's configuration: the share edition only reads the
user's groups through the controller socket and switches nodes inside them.

Layout on the friend's Mac:
    ~/Library/Application Support/SteadyRoute/         program, state.json, config/
    ~/Library/Application Support/SteadyRoute-backups/ last 3 versions, for automatic rollback
    ~/Library/Logs/SteadyRoute/                        logs (bounded by the router itself)
    ~/Library/LaunchAgents/com.steadyroute.share.plist starts at login, restarts if it stops
"""

import argparse
import http.client
import json
import os
import pathlib
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

LABEL = "com.steadyroute.share"
PORT = 17654
KEEP_BACKUPS = 3
HEALTH_TIMEOUT_SECONDS = 40
MIN_PYTHON = (3, 9)
# Files that belong to the user and survive upgrades.
PRESERVED = ("state.json", "config/route-policies.json")
CLASH_APPS = ("/Applications/Clash Verge.app", "~/Applications/Clash Verge.app")


class InstallError(RuntimeError):
    """A problem the user has to fix; the message is shown as is."""


def say(message=""):
    print(message, flush=True)


class Paths(object):
    def __init__(self, home):
        home = pathlib.Path(home)
        self.home = home
        self.app = home / "Library" / "Application Support" / "SteadyRoute"
        self.backups = home / "Library" / "Application Support" / "SteadyRoute-backups"
        self.logs = home / "Library" / "Logs" / "SteadyRoute"
        self.agents = home / "Library" / "LaunchAgents"
        self.plist = self.agents / (LABEL + ".plist")


def controller_sockets(uid=None, tmpdir=None):
    """Where Clash Verge exposes the Mihomo controller (same list as the router)."""
    uid = os.getuid() if uid is None else uid
    tmpdir = tempfile.gettempdir() if tmpdir is None else tmpdir
    return [
        "/var/run/clash-verge-service/users/%d/verge-mihomo.sock" % uid,
        os.path.join(tmpdir, "verge-mihomo.sock"),
        "/tmp/verge/verge-mihomo.sock",
    ]


def port_is_free(port=PORT):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Same option the dashboard server binds with, so a just-closed port in TIME_WAIT counts as free.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def fetch_status(port=PORT, timeout=3):
    """The router's own status API; None while it is not answering yet."""
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        connection.request("GET", "/api/status", headers={"Host": "127.0.0.1:%d" % port})
        response = connection.getresponse()
        body = response.read()
        connection.close()
    except (OSError, http.client.HTTPException):
        return None
    if response.status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return None


def run_command(argv):
    completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return completed.returncode, (completed.stdout + completed.stderr).decode("utf-8", "replace").strip()


def python_for_service():
    """/usr/bin/python3 survives command line tools updates; fall back to this interpreter."""
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def read_version(directory):
    try:
        return (pathlib.Path(directory) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def valid_share_config(path):
    try:
        config = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and config.get("profile") == "auto_lock"


class Installer(object):
    """Every side effect goes through an injectable hook so the whole flow runs in tests."""

    def __init__(self, package_dir, home=None, uid=None, system=None, run=run_command,
                 status=fetch_status, port_free=port_is_free, sleep=time.sleep, python=None,
                 sockets=None, clock=time.time):
        self.package = pathlib.Path(package_dir)
        self.source = self.package / "app"
        self.paths = Paths(home or pathlib.Path.home())
        self.uid = os.getuid() if uid is None else uid
        self.system = system or os.uname().sysname
        self.run = run
        self.status = status
        self.port_free = port_free
        self.sleep = sleep
        self.python = python or python_for_service()
        self.sockets = sockets if sockets is not None else controller_sockets(self.uid)
        self.clock = clock
        self.domain = "gui/%d" % self.uid

    # ---------------------------------------------------------------- checks
    def preflight(self):
        """Raise InstallError for blockers; return warnings the user should read."""
        if self.system != "Darwin":
            raise InstallError("分享版目前只支持 macOS（Windows 版之后再做）。")
        if sys.version_info[:2] < MIN_PYTHON:
            raise InstallError("需要 Python 3.9 或更新版本，当前是 %d.%d。" % sys.version_info[:2])
        if not (self.source / "weighted_router.py").exists() or not read_version(self.source):
            raise InstallError("安装包不完整：找不到 app/weighted_router.py 或 VERSION，请重新下载。")
        if not valid_share_config(self.source / "config" / "route-policies.json"):
            raise InstallError("安装包里的配置不是分享版配置，请重新下载。")
        other = self.other_installs()
        if other:
            raise InstallError("这台 Mac 上已经有另一份稳航在运行（%s），两份不能同时运行。" % other[0])
        warnings = []
        if not any(pathlib.Path(os.path.expanduser(path)).exists() for path in CLASH_APPS):
            warnings.append("没在“应用程序”里找到 Clash Verge。稳航需要 Clash Verge 才能工作。")
        if not any(os.path.exists(path) for path in self.sockets):
            warnings.append("Clash Verge 现在好像没在运行。先装好，打开 Clash Verge 后稳航会自动连上。")
        return warnings

    def other_installs(self):
        """LaunchAgents that run SteadyRoute under another label (e.g. the author's own setup)."""
        found = []
        if not self.paths.agents.is_dir():
            return found
        for path in sorted(self.paths.agents.glob("*.plist")):
            if path.name == self.paths.plist.name:
                continue
            try:
                with open(str(path), "rb") as handle:
                    data = plistlib.load(handle)
            except Exception:
                continue
            arguments = data.get("ProgramArguments") or []
            if any(str(item).endswith("weighted_router.py") for item in arguments):
                found.append(str(data.get("Label") or path.stem))
        return found

    # ---------------------------------------------------------------- launchd
    def stop_service(self):
        if self.paths.plist.exists():
            self.run(["launchctl", "bootout", self.domain, str(self.paths.plist)])
        for _ in range(20):
            if self.port_free():
                return True
            self.sleep(0.5)
        return self.port_free()

    def start_service(self):
        code, output = self.run(["launchctl", "bootstrap", self.domain, str(self.paths.plist)])
        if code != 0:
            raise InstallError("开机自启注册失败：%s" % (output or code))

    def write_plist(self):
        self.paths.agents.mkdir(parents=True, exist_ok=True)
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        data = {
            "Label": LABEL,
            "ProgramArguments": [self.python, str(self.paths.app / "weighted_router.py"), "--daemon"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "EnvironmentVariables": {
                "PYTHONUNBUFFERED": "1",
                "STEADYROUTE_BASE_DIR": str(self.paths.app),
                "STEADYROUTE_LOG_DIR": str(self.paths.logs),
            },
            # The router logs to its own rotated files; these only catch start-up crashes.
            "StandardOutPath": str(self.paths.logs / "launchd.log"),
            "StandardErrorPath": str(self.paths.logs / "launchd.log"),
        }
        temporary = self.paths.plist.with_suffix(".plist.tmp")
        with open(str(temporary), "wb") as handle:
            plistlib.dump(data, handle)
        os.replace(str(temporary), str(self.paths.plist))

    # ---------------------------------------------------------------- files
    def backup_current(self):
        if not (self.paths.app / "weighted_router.py").exists():
            return None
        version = read_version(self.paths.app) or "unknown"
        target = self.paths.backups / ("%s-%d" % (version, int(self.clock())))
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(self.paths.app), str(target), ignore=shutil.ignore_patterns("router.lock", "__pycache__"))
        backups = sorted(path for path in self.paths.backups.iterdir() if path.is_dir())
        for old in sorted(backups, key=lambda path: path.stat().st_mtime)[:-KEEP_BACKUPS]:
            shutil.rmtree(str(old), ignore_errors=True)
        return target

    def install_files(self):
        """Copy the program in; keep the user's state and config."""
        app = self.paths.app
        app.mkdir(parents=True, exist_ok=True)
        kept_config = valid_share_config(app / "config" / "route-policies.json")
        for source in sorted(self.source.rglob("*")):
            relative = source.relative_to(self.source)
            if source.is_dir() or "__pycache__" in relative.parts:
                continue
            if relative.as_posix() in PRESERVED and (app / relative).exists():
                if relative.as_posix() != "config/route-policies.json" or kept_config:
                    continue
            target = app / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".new")
            shutil.copy2(str(source), str(temporary))
            os.replace(str(temporary), str(target))
        shutil.rmtree(str(app / "__pycache__"), ignore_errors=True)
        return kept_config

    def restore(self, backup):
        keep = {name: (self.paths.app / name).read_bytes() for name in ("state.json",)
                if (self.paths.app / name).exists()}
        shutil.rmtree(str(self.paths.app), ignore_errors=True)
        shutil.copytree(str(backup), str(self.paths.app))
        for name, content in keep.items():
            (self.paths.app / name).write_bytes(content)

    # ---------------------------------------------------------------- health
    def wait_healthy(self, version):
        deadline = self.clock() + HEALTH_TIMEOUT_SECONDS
        last = None
        while True:
            snapshot = self.status()
            service = (snapshot or {}).get("service") or {}
            if service.get("version") == version and service.get("profile") == "auto_lock":
                return snapshot
            if snapshot:
                last = service.get("version")
            if self.clock() >= deadline:
                if last:
                    raise InstallError("看板在运行，但版本是 %s，不是刚装的 %s。" % (last, version))
                raise InstallError("稳航 %d 秒内没有启动成功。" % HEALTH_TIMEOUT_SECONDS)
            self.sleep(1)

    # ---------------------------------------------------------------- commands
    def install(self, open_dashboard=True):
        warnings = self.preflight()
        version = read_version(self.source)
        previous = read_version(self.paths.app) if (self.paths.app / "weighted_router.py").exists() else None
        action = "安装" if not previous else "重新安装" if previous == version else "升级（当前 v%s）" % previous
        say("稳航分享版 v%s %s" % (version, action))
        for warning in warnings:
            say("  注意：" + warning)
        if not self.stop_service():
            raise InstallError("端口 %d 被别的程序占用了，稳航的看板用不了这个端口。" % PORT)
        backup = self.backup_current()
        kept = self.install_files()
        if kept:
            say("  保留了你原来的配置 config/route-policies.json")
        self.write_plist()
        try:
            self.start_service()
            snapshot = self.wait_healthy(version)
        except InstallError as error:
            self.rollback(backup)
            raise InstallError("%s%s" % (error, "已恢复到 v%s。" % previous if backup else "已停止，没有留下运行中的服务。"))
        say("  已启动，开机会自动运行。")
        self.describe(snapshot)
        url = "http://127.0.0.1:%d/" % PORT
        say("看板：%s" % url)
        if open_dashboard:
            self.run(["open", url])
        return snapshot

    def rollback(self, backup):
        self.run(["launchctl", "bootout", self.domain, str(self.paths.plist)])
        if backup is None:
            try:
                self.paths.plist.unlink()
            except OSError:
                pass
            return
        self.restore(backup)
        self.write_plist()
        self.run(["launchctl", "bootstrap", self.domain, str(self.paths.plist)])

    def describe(self, snapshot):
        groups = (snapshot or {}).get("groups") or []
        idle = ((snapshot or {}).get("service") or {}).get("auto_lock_idle") or {}
        if not ((snapshot or {}).get("service") or {}).get("controller_connected"):
            say("  还没连上 Clash Verge；打开 Clash Verge 后会自动接管。")
            return
        for group in groups:
            lock = group.get("auto_lock") or {}
            say("  %s：锁定%s，%s 个家宽候选" % (group.get("name"), lock.get("country_label", "?"), lock.get("candidates", "?")))
        for name, status in idle.items():
            reason = {"no_residential": "这个国家没有家宽节点，不切换", "unknown_country": "认不出节点的国家",
                      "paused": "选的是另一个分组，暂停"}.get(status.get("status"), "未接管")
            say("  %s：%s" % (name, reason))
        if not groups and not idle:
            say("  Clash 里没有直接选中节点的分组，稳航暂时没有可接管的线路。")

    def uninstall(self, purge=False):
        if self.paths.plist.exists():
            self.run(["launchctl", "bootout", self.domain, str(self.paths.plist)])
            self.paths.plist.unlink()
        shutil.rmtree(str(self.paths.app), ignore_errors=True)
        shutil.rmtree(str(self.paths.backups), ignore_errors=True)
        if purge:
            shutil.rmtree(str(self.paths.logs), ignore_errors=True)
            say("稳航已卸载，日志也已删除。")
        else:
            say("稳航已卸载。日志留在 %s，不需要可以直接删掉。" % self.paths.logs)
        say("Clash Verge 里的分组和节点选择都没动过。")

    def report(self):
        snapshot = self.status()
        installed = read_version(self.paths.app)
        if not installed:
            say("稳航分享版没有安装。")
            return 1
        if snapshot is None:
            say("已安装 v%s，但现在没有运行。可以重新运行安装程序。" % installed)
            return 1
        say("稳航分享版 v%s 正在运行，看板 http://127.0.0.1:%d/" % (snapshot["service"].get("version"), PORT))
        self.describe(snapshot)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="稳航分享版安装器")
    parser.add_argument("command", choices=("install", "uninstall", "status"))
    parser.add_argument("--no-open", action="store_true", help="装好后不自动打开看板")
    parser.add_argument("--purge", action="store_true", help="卸载时连日志一起删除")
    args = parser.parse_args(argv)
    installer = Installer(pathlib.Path(__file__).resolve().parent.parent)
    try:
        if args.command == "install":
            installer.install(open_dashboard=not args.no_open)
        elif args.command == "uninstall":
            installer.uninstall(purge=args.purge)
        else:
            return installer.report()
    except InstallError as error:
        say()
        say("没有完成：%s" % error)
        say("日志在 %s" % installer.paths.logs)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
