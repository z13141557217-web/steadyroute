#!/usr/bin/env python3
"""稳航 SteadyRoute 安装器（macOS · Clash Verge）。仓库和安装包里是同一个文件。

    python3 scripts/installer.py install     安装或升级；状态、历史和设置都保留
    python3 scripts/installer.py uninstall   停止、删除，并把稳航写进 Clash 的内容全部去掉
    python3 scripts/installer.py status      看看有没有在运行

Standard library only (Python 3.9, the version macOS ships with the command line tools).
It works from a git clone and from the release zip, which share the same layout:
    VERSION, src/steadyroute/…, config/route-policies.default.json, scripts/installer.py

On the Mac:
    ~/Library/Application Support/SteadyRoute/          program, state.json, config/
    ~/Library/Application Support/SteadyRoute-backups/  last 3 versions + retired old agents
    ~/Library/Logs/SteadyRoute/                         logs (bounded by the router itself)
    ~/Library/LaunchAgents/com.steadyroute.plist        starts at login, restarts if it stops

An older install (any LaunchAgent that runs weighted_router.py, e.g. the pre-0.5.1 author
setup or the 0.5.0 share edition) is migrated: its state, history and logs are carried over,
its settings are converted, and it is only retired once the new service is healthy. If the
new service does not come up, the old one is started again untouched.

Installing never changes Clash. The AI line (and the Taiwan / Hong Kong lines of a migrated
setup) is written into Clash only from the settings page, after showing what will change.
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

LABEL = "com.steadyroute"
PORT = 17654
KEEP_BACKUPS = 3
HEALTH_TIMEOUT_SECONDS = 40
MIN_PYTHON = (3, 9)
CLASH_APP_ID = "io.github.clash-verge-rev.clash-verge-rev"
CLASH_APPS = ("/Applications/Clash Verge.app", "~/Applications/Clash Verge.app")
APP_FILES = (
    "weighted_router.py", "state_contract.py", "route_policy.py", "candidate_registry.py",
    "health_model.py", "runtime_metrics.py", "logging_setup.py", "node_catalog.py",
    "regions.py", "auto_lock.py", "ai_rules.py", "ai_line.py", "ai_check.py",
    "clash_profile.py", "settings_service.py",
    "dashboard.html", "settings.html", "nodes.html", "guide.html", "changelog.html", "pages.css",
)
DEFAULT_CONFIG = "config/route-policies.default.json"
AI_HINTS = ("chatgpt.com", "claude.ai", "openai.com", "anthropic.com")


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
    uid = os.getuid() if uid is None else uid
    tmpdir = tempfile.gettempdir() if tmpdir is None else tmpdir
    return ["/var/run/clash-verge-service/users/%d/verge-mihomo.sock" % uid,
            os.path.join(tmpdir, "verge-mihomo.sock"), "/tmp/verge/verge-mihomo.sock"]


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


def read_version(path):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_current_config(config):
    return isinstance(config, dict) and config.get("profile") == "auto_lock" and config.get("schema_version") == 1


def convert_legacy_config(old, default):
    """Settings for a pre-0.5.1 fixed Taiwan / Hong Kong setup, in the unified format.

    Nothing is written to Clash here: the old lines are offered on the settings page as a
    suggested AI line (keeping the group's name, so the user's own rules keep working).
    """
    config = json.loads(json.dumps(default))
    if is_current_config(old):
        return old
    if not isinstance(old, dict) or not isinstance(old.get("policies"), list):
        return config
    per_group, lines, legacy = {}, [], []
    ai = None
    for policy in old["policies"]:
        name, region = policy.get("group_name"), policy.get("region")
        if not name or not region:
            continue
        urls = [url for url in policy.get("business_test_urls") or [] if isinstance(url, str) and url.startswith("https://")]
        if urls:
            per_group[name] = urls
        if policy.get("discovery_group_name"):
            legacy.append(policy["discovery_group_name"])
        if ai is None and any(hint in url for url in urls for hint in AI_HINTS) and region not in ("HK", "MO"):
            ai = {"enabled": True, "group_name": name, "country": region}
        else:
            lines.append({"group_name": name, "country": region})
    config.setdefault("auto_lock", {})["group_business_urls"] = per_group
    # Conservative on upgrade: SteadyRoute keeps switching exactly the groups it switched before;
    # every other group stays with Clash's own choice until the user turns it on in settings.
    config["auto_lock"]["include_groups"] = [name for name in
                                             ([ai["group_name"]] if ai else []) + [line["group_name"] for line in lines]]
    config["migration"] = {
        "from": "fixed", "at": int(time.time()),
        "ai_line": ai, "managed_lines": lines, "legacy_group_names": legacy,
    }
    return config


class Installer(object):
    """Every side effect goes through an injectable hook so the whole flow runs in tests."""

    def __init__(self, root, home=None, uid=None, system=None, run=run_command, status=fetch_status,
                 port_free=port_is_free, sleep=time.sleep, python=None, sockets=None, clock=time.time,
                 ask=None, service_factory=None):
        self.root = pathlib.Path(root)
        self.source = self.root / "src" / "steadyroute"
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
        self.ask = ask
        self.service_factory = service_factory
        self.domain = "gui/%d" % self.uid

    # ---------------------------------------------------------------- checks
    def version(self):
        return read_version(self.root / "VERSION")

    def preflight(self):
        if self.system != "Darwin":
            raise InstallError("稳航目前只支持 macOS（Windows 版之后再做）。")
        if sys.version_info[:2] < MIN_PYTHON:
            raise InstallError("需要 Python 3.9 或更新版本，当前是 %d.%d。" % sys.version_info[:2])
        missing = [name for name in APP_FILES if not (self.source / name).is_file()]
        if missing or not self.version():
            raise InstallError("安装文件不完整（缺少 %s），请重新下载或 git pull。" % (missing[0] if missing else "VERSION"))
        if not is_current_config(read_json(self.root / DEFAULT_CONFIG)):
            raise InstallError("默认配置 %s 损坏，请重新下载。" % DEFAULT_CONFIG)
        warnings = []
        if not any(pathlib.Path(os.path.expanduser(path)).exists() for path in CLASH_APPS):
            warnings.append("没在“应用程序”里找到 Clash Verge。稳航需要 Clash Verge 才能工作。")
        if not any(os.path.exists(path) for path in self.sockets):
            warnings.append("Clash Verge 现在好像没在运行。装好后打开 Clash Verge，稳航会自动连上。")
        return warnings

    def legacy_agents(self):
        """Older SteadyRoute LaunchAgents (another label, or another folder)."""
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
            script = next((str(item) for item in data.get("ProgramArguments") or []
                           if str(item).endswith("weighted_router.py")), None)
            if not script:
                continue
            env = data.get("EnvironmentVariables") or {}
            app_dir = pathlib.Path(script).parent
            found.append({
                "label": str(data.get("Label") or path.stem), "plist": path, "app": app_dir,
                "base": pathlib.Path(env.get("STEADYROUTE_BASE_DIR") or app_dir),
                "logs": pathlib.Path(env.get("STEADYROUTE_LOG_DIR") or (
                    self.paths.home / "Library" / "Logs" / "Clash-Verge-Stability-Router")),
            })
        return found

    # ---------------------------------------------------------------- launchd
    def bootout(self, plist):
        if pathlib.Path(plist).exists():
            self.run(["launchctl", "bootout", self.domain, str(plist)])

    def wait_port(self):
        for _ in range(20):
            if self.port_free():
                return True
            self.sleep(0.5)
        return self.port_free()

    def bootstrap(self, plist):
        code, output = self.run(["launchctl", "bootstrap", self.domain, str(plist)])
        if code != 0:
            raise InstallError("开机自启注册失败：%s" % (output or code))

    def write_plist(self):
        self.paths.agents.mkdir(parents=True, exist_ok=True)
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        data = {
            "Label": LABEL,
            "ProgramArguments": [self.python, str(self.paths.app / "weighted_router.py"), "--daemon"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ProcessType": "Background",
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
        version = read_version(self.paths.app / "VERSION") or "unknown"
        target = self.paths.backups / ("%s-%d" % (version, int(self.clock())))
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(self.paths.app), str(target), ignore=shutil.ignore_patterns("router.lock", "__pycache__"))
        versions = sorted((p for p in self.paths.backups.iterdir() if p.is_dir() and p.name != "legacy"),
                          key=lambda p: p.stat().st_mtime)
        for old in versions[:-KEEP_BACKUPS]:
            shutil.rmtree(str(old), ignore_errors=True)
        return target

    def install_files(self, legacy):
        """Program in, user data kept. Returns a note about the settings used."""
        app = self.paths.app
        (app / "config").mkdir(parents=True, exist_ok=True)
        for name in APP_FILES:
            target = app / name
            temporary = target.with_name(target.name + ".new")
            shutil.copy2(str(self.source / name), str(temporary))
            os.replace(str(temporary), str(target))
        shutil.copy2(str(self.root / "VERSION"), str(app / "VERSION"))
        shutil.rmtree(str(app / "__pycache__"), ignore_errors=True)
        config_path = app / "config" / "route-policies.json"
        default = read_json(self.root / DEFAULT_CONFIG)
        current = read_json(config_path)
        if is_current_config(current):
            return "kept"
        old = None
        for agent in legacy:
            old = read_json(agent["app"] / "config" / "route-policies.json")
            if old:
                break
        converted = convert_legacy_config(old if old else current, default)
        config_path.write_text(json.dumps(converted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "migrated" if converted.get("migration") else "default"

    def carry_over(self, legacy):
        """State and logs of an older install, when this folder has none yet."""
        notes = []
        for agent in legacy:
            state = agent["base"] / "state.json"
            if state.is_file() and not (self.paths.app / "state.json").exists() and agent["base"] != self.paths.app:
                shutil.copy2(str(state), str(self.paths.app / "state.json"))
                notes.append("状态和节点历史")
            if agent["logs"].is_dir() and agent["logs"] != self.paths.logs and not (self.paths.logs / "router.log").exists():
                self.paths.logs.mkdir(parents=True, exist_ok=True)
                for item in agent["logs"].iterdir():
                    if item.is_file() and not item.name.startswith("bootstrap"):
                        shutil.copy2(str(item), str(self.paths.logs / item.name))
                notes.append("日志")
        return notes

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
        version = self.version()
        legacy = self.legacy_agents()
        previous = read_version(self.paths.app / "VERSION") if (self.paths.app / "weighted_router.py").exists() else None
        action = ("迁移旧版（%s）" % "、".join(agent["label"] for agent in legacy) if legacy
                  else "安装" if not previous else "重新安装" if previous == version else "升级（当前 v%s）" % previous)
        say("稳航 SteadyRoute v%s %s" % (version, action))
        for warning in warnings:
            say("  注意：" + warning)
        for agent in legacy:
            self.bootout(agent["plist"])
        self.bootout(self.paths.plist)
        if not self.wait_port():
            self._restart_legacy(legacy)
            raise InstallError("端口 %d 被别的程序占用了，稳航的看板用不了这个端口。" % PORT)
        backup = self.backup_current()
        settings = self.install_files(legacy)
        carried = self.carry_over(legacy)
        if settings == "kept":
            say("  保留了原来的设置")
        if carried:
            say("  已带过来：%s" % "、".join(carried))
        self.write_plist()
        try:
            self.bootstrap(self.paths.plist)
            snapshot = self.wait_healthy(version)
        except InstallError as error:
            self.rollback(backup, legacy)
            where = ("已恢复到 v%s。" % previous) if backup else ("旧版已重新启动。" if legacy else "没有留下运行中的服务。")
            raise InstallError("%s%s" % (error, where))
        retired = self._retire_legacy(legacy)
        say("  已启动，开机会自动运行。")
        if retired:
            say("  旧版的开机自启已停用（保存在 %s）" % retired)
        self.describe(snapshot)
        url = "http://127.0.0.1:%d/" % PORT
        if settings == "migrated":
            say("  下一步：在设置页查看并确认 AI 专线的改动，确认后才会写入 Clash。")
            url += "settings"
        elif settings == "default" and self.ask and self.ask("要启用 AI 家宽专线吗？（可以以后在设置页再开）[y/N] "):
            url += "settings#ai-line"
        say("看板：%s" % url)
        if open_dashboard:
            self.run(["open", url])
        return snapshot

    def _restart_legacy(self, legacy):
        for agent in legacy:
            if agent["plist"].exists():
                self.run(["launchctl", "bootstrap", self.domain, str(agent["plist"])])

    def _retire_legacy(self, legacy):
        if not legacy:
            return None
        folder = self.paths.backups / "legacy"
        folder.mkdir(parents=True, exist_ok=True)
        for agent in legacy:
            if agent["plist"].exists():
                os.replace(str(agent["plist"]), str(folder / agent["plist"].name))
        return folder

    def rollback(self, backup, legacy=()):
        self.run(["launchctl", "bootout", self.domain, str(self.paths.plist)])
        if backup is None:
            try:
                self.paths.plist.unlink()
            except OSError:
                pass
        else:
            self.restore(backup)
            self.write_plist()
            self.run(["launchctl", "bootstrap", self.domain, str(self.paths.plist)])
        self._restart_legacy(legacy)

    def describe(self, snapshot):
        service = (snapshot or {}).get("service") or {}
        groups = (snapshot or {}).get("groups") or []
        idle = service.get("auto_lock_idle") or {}
        if not service.get("controller_connected"):
            say("  还没连上 Clash Verge；打开 Clash Verge 后会自动接管。")
            return
        for group in groups:
            lock = group.get("auto_lock") or {}
            say("  %s：锁定%s，%s 个家宽候选" % (group.get("name"), lock.get("country_label", "?"), lock.get("candidates", "?")))
        for name, status in idle.items():
            reason = {"no_residential": "无家宽节点，不接管切换", "unknown_country": "国家未识别",
                      "paused": "当前选择另一个分组，暂停接管"}.get(status.get("status"), "未接管")
            say("  %s：%s" % (name, reason))
        if not groups and not idle:
            say("  Clash 里没有直接选中节点的分组，暂无接管线路。")

    def remove_clash_changes(self):
        """Take the AI line and managed lines out of Clash, using the installed modules.

        Returns the removed group names, or None when nothing of ours is in Clash.
        """
        applied = read_json(self.paths.app / "clash-applied.json")
        if not applied or not applied.get("groups"):
            return None
        service = (self.service_factory or self.installed_service)()
        return service.remove_all()

    def clash_socket(self):
        return next((path for path in self.sockets if os.path.exists(path)), None)

    def installed_service(self):
        socket_path = self.clash_socket()
        if not socket_path:
            raise InstallError("Clash Verge 没有运行，无法撤销稳航写入 Clash 的专线。请打开 Clash Verge 后再运行一次卸载。")
        sys.path.insert(0, str(self.paths.app))
        try:
            import importlib
            settings_service = importlib.import_module("settings_service")
        finally:
            sys.path.remove(str(self.paths.app))
        return settings_service.SettingsService(
            self.paths.app / "config" / "route-policies.json", self.paths.app, unix_controller(socket_path),
            clash_home=self.paths.home / "Library" / "Application Support" / CLASH_APP_ID)

    def uninstall(self, purge=False):
        applied = read_json(self.paths.app / "clash-applied.json") or {}
        if applied.get("groups") and not self.service_factory and not self.clash_socket():
            raise InstallError("Clash Verge 没有运行，无法撤销稳航写入 Clash 的专线。请打开 Clash Verge 后再运行一次卸载。")
        # Stop first, so the service cannot write the lines back while they are being removed.
        if self.paths.plist.exists():
            self.run(["launchctl", "bootout", self.domain, str(self.paths.plist)])
        try:
            removed = self.remove_clash_changes()
        except Exception as error:
            raise InstallError("撤销 Clash 里的专线失败（%s）。稳航已停止，程序文件保留，修复后可再运行一次卸载。" % error)
        if removed:
            say("已从 Clash 撤销：%s；原有的分组和规则已恢复。" % "、".join(removed))
        if self.paths.plist.exists():
            self.paths.plist.unlink()
        shutil.rmtree(str(self.paths.app), ignore_errors=True)
        shutil.rmtree(str(self.paths.backups), ignore_errors=True)
        if purge:
            shutil.rmtree(str(self.paths.logs), ignore_errors=True)
            say("稳航已卸载，日志也已删除。")
        else:
            say("稳航已卸载。日志保留在 %s，不需要可以直接删除。" % self.paths.logs)

    def report(self):
        snapshot = self.status()
        installed = read_version(self.paths.app / "VERSION")
        if not installed:
            say("稳航没有安装。")
            return 1
        if snapshot is None:
            say("已安装 v%s，但现在没有运行。可以重新运行安装。" % installed)
            return 1
        say("稳航 v%s 正在运行，看板 http://127.0.0.1:%d/" % (snapshot["service"].get("version"), PORT))
        self.describe(snapshot)
        return 0


def unix_controller(socket_path):
    """(method, path, payload) -> (status, body) over Clash Verge's controller socket."""
    def call(method, path, payload=None):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        head = ["%s %s HTTP/1.0" % (method, path), "Host: localhost"]
        if body:
            head += ["Content-Type: application/json", "Content-Length: %d" % len(body)]
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(30)
        try:
            client.connect(socket_path)
            client.sendall(("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + body)
            data = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            client.close()
        header, _, content = data.partition(b"\r\n\r\n")
        status = int(header.split(b" ")[1]) if header else 0
        return status, content.decode("utf-8", "replace")
    return call


def main(argv=None):
    parser = argparse.ArgumentParser(description="稳航 SteadyRoute 安装器")
    parser.add_argument("command", choices=("install", "uninstall", "status"))
    parser.add_argument("--no-open", action="store_true", help="装好后不自动打开看板")
    parser.add_argument("--purge", action="store_true", help="卸载时连日志一起删除")
    args = parser.parse_args(argv)

    def ask(question):
        if not sys.stdin.isatty():
            return False
        try:
            return input(question).strip().lower() in ("y", "yes", "是")
        except EOFError:
            return False

    installer = Installer(pathlib.Path(__file__).resolve().parent.parent, ask=ask)
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
