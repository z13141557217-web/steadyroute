"""v0.5.0 share edition: package builder and the macOS installer, with launchd and the
status API replaced by fakes."""

import importlib.util
import json
import os
import pathlib
import plistlib
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_share = load("build_share", ROOT / "scripts" / "share" / "build_share.py")
installer = load("share_installer", ROOT / "scripts" / "share" / "installer.py")
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


class FakeLaunchd(object):
    """Records launchctl calls; 'running' mirrors whether the agent is loaded."""

    def __init__(self, test):
        self.test = test
        self.calls = []
        self.running = False
        self.bootstrap_code = 0

    def run(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["launchctl", "bootstrap"]:
            self.running = self.bootstrap_code == 0
            return self.bootstrap_code, "" if self.bootstrap_code == 0 else "Bootstrap failed: 5"
        if argv[:2] == ["launchctl", "bootout"]:
            self.running = False
        return 0, ""


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tree, cls.archive = build_share.build(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_package_holds_program_config_and_installer_only(self):
        names = {path.relative_to(self.tree).as_posix() for path in self.tree.rglob("*") if path.is_file()}
        for required in ("app/weighted_router.py", "app/auto_lock.py", "app/regions.py", "app/dashboard.html",
                         "app/VERSION", "app/config/route-policies.json", "tools/installer.py",
                         "install.command", "uninstall.command", "使用说明.txt"):
            self.assertIn(required, names)
        for absent in ("app/acceptance_dashboard.html", "app/candidate_dashboard.html", "app/clash_group_deploy.py",
                       "app/config/groups.yaml", "app/state.json"):
            self.assertNotIn(absent, names)
        config = json.loads((self.tree / "app/config/route-policies.json").read_text(encoding="utf-8"))
        self.assertEqual((config["profile"], config["policies"]), ("auto_lock", []))

    def test_package_has_no_personal_data(self):
        self.assertEqual(build_share.check_personal_data(self.tree), [])
        author = (ROOT / "config" / "route-policies.json").read_text(encoding="utf-8")
        node = json.loads(author)["policies"][0]["static_candidates"][0]
        for path in self.tree.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".html", ".json", ".txt", ".command"):
                self.assertNotIn(node, path.read_text(encoding="utf-8"), path)

    def test_personal_data_check_catches_planted_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.py").write_text('BASE = "/Users/alice/Library"\n', encoding="utf-8")
            (root / "b.json").write_text('{"url": "https://example.com/api/v1/client/subscribe?flag=clash"}', encoding="utf-8")
            (root / "state.json").write_text("{}", encoding="utf-8")
            reasons = {path: reason for path, reason in build_share.check_personal_data(root)}
            self.assertEqual(set(reasons), {"a.py", "b.json", "state.json"})

    def test_packaged_router_loads_the_share_profile(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.dict(os.environ, {"STEADYROUTE_BASE_DIR": base}):
            os.environ.pop("STEADYROUTE_POLICY_CONFIG", None)
            sys.path.insert(0, str(self.tree / "app"))
            try:
                router = load("packaged_router", self.tree / "app" / "weighted_router.py")
            finally:
                sys.path.remove(str(self.tree / "app"))
            self.assertEqual(router.PROFILE, "auto_lock")
            self.assertEqual(router.read_service_version(), VERSION)

    def test_zip_keeps_command_files_executable(self):
        with zipfile.ZipFile(str(self.archive)) as bundle:
            info = bundle.getinfo("SteadyRoute-share-v%s/install.command" % VERSION)
            self.assertTrue((info.external_attr >> 16) & stat.S_IXUSR)


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tree, _archive = build_share.build(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.home_dir = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.home_dir.name)
        self.launchd = FakeLaunchd(self)
        self.version = VERSION
        self.clock = [1790409600.0]
        self.printed = []

    def tearDown(self):
        self.home_dir.cleanup()

    def status(self):
        if not self.launchd.running:
            return None
        return {"service": {"version": self.version, "profile": "auto_lock", "controller_connected": True,
                            "auto_lock_idle": {}},
                "groups": [{"name": "🚀 节点选择", "auto_lock": {"country_label": "美国", "candidates": 3}}]}

    def make(self, **overrides):
        def sleep(seconds):
            self.clock[0] += seconds
        options = dict(home=self.home, uid=501, system="Darwin", run=self.launchd.run, status=self.status,
                       port_free=lambda: not self.launchd.running, sleep=sleep, python="/usr/bin/python3",
                       sockets=[], clock=lambda: self.clock[0])
        options.update(overrides)
        return installer.Installer(self.tree, **options)

    def install(self, **overrides):
        with mock.patch.object(installer, "say", side_effect=lambda message="": self.printed.append(message)):
            return self.make(**overrides).install(open_dashboard=False)

    @property
    def app(self):
        return self.home / "Library/Application Support/SteadyRoute"

    def test_fresh_install_places_files_and_a_per_user_launch_agent(self):
        self.install()
        self.assertTrue((self.app / "weighted_router.py").exists())
        self.assertEqual((self.app / "VERSION").read_text(encoding="utf-8").strip(), VERSION)
        plist_path = self.home / "Library/LaunchAgents/com.steadyroute.share.plist"
        with open(str(plist_path), "rb") as handle:
            data = plistlib.load(handle)
        self.assertEqual(data["Label"], "com.steadyroute.share")
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", str(self.app / "weighted_router.py"), "--daemon"])
        self.assertEqual(data["EnvironmentVariables"]["STEADYROUTE_BASE_DIR"], str(self.app))
        self.assertEqual(data["EnvironmentVariables"]["STEADYROUTE_LOG_DIR"], str(self.home / "Library/Logs/SteadyRoute"))
        self.assertTrue(data["KeepAlive"] and data["RunAtLoad"])
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(plist_path)], self.launchd.calls)
        self.assertTrue(any("锁定美国" in line for line in self.printed), self.printed)

    def test_upgrade_keeps_state_and_edited_config_and_backs_up(self):
        self.install()
        (self.app / "state.json").write_text('{"schema_version": 2, "mine": true}', encoding="utf-8")
        config = json.loads((self.app / "config/route-policies.json").read_text(encoding="utf-8"))
        config["auto_lock"]["exclude_groups"] = ["🎬 流媒体"]
        (self.app / "config/route-policies.json").write_text(json.dumps(config), encoding="utf-8")
        self.clock[0] += 60
        self.install()
        self.assertIn('"mine": true', (self.app / "state.json").read_text(encoding="utf-8"))
        kept = json.loads((self.app / "config/route-policies.json").read_text(encoding="utf-8"))
        self.assertEqual(kept["auto_lock"]["exclude_groups"], ["🎬 流媒体"])
        backups = list((self.home / "Library/Application Support/SteadyRoute-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "state.json").exists())

    def test_broken_config_is_replaced_on_upgrade(self):
        self.install()
        (self.app / "config/route-policies.json").write_text('{"profile": "fixed"}', encoding="utf-8")
        self.install()
        config = json.loads((self.app / "config/route-policies.json").read_text(encoding="utf-8"))
        self.assertEqual(config["profile"], "auto_lock")

    def test_only_three_backups_are_kept(self):
        for _ in range(6):
            self.install()
            self.clock[0] += 60
        backups_dir = self.home / "Library/Application Support/SteadyRoute-backups"
        self.assertEqual(len(list(backups_dir.iterdir())), 3)

    def test_failed_start_on_upgrade_restores_the_previous_version(self):
        self.install()
        (self.app / "weighted_router.py").write_text("# previous release\n", encoding="utf-8")
        (self.app / "VERSION").write_text("0.4.9\n", encoding="utf-8")
        self.version = "0.4.9"   # the new files never answer with the new version
        with self.assertRaises(installer.InstallError) as caught:
            self.install()
        self.assertIn("已恢复到 v0.4.9", str(caught.exception))
        self.assertEqual((self.app / "weighted_router.py").read_text(encoding="utf-8"), "# previous release\n")
        self.assertEqual(self.launchd.calls[-1][:2], ["launchctl", "bootstrap"], "the old version is started again")

    def test_failed_fresh_install_leaves_nothing_running(self):
        self.launchd.bootstrap_code = 5
        with self.assertRaises(installer.InstallError) as caught:
            self.install()
        self.assertIn("开机自启注册失败", str(caught.exception))
        self.assertFalse((self.home / "Library/LaunchAgents/com.steadyroute.share.plist").exists())
        self.assertFalse(self.launchd.running)

    def test_refuses_next_to_another_steadyroute_agent(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        with open(str(agents / "com.example.router.plist"), "wb") as handle:
            plistlib.dump({"Label": "com.example.router",
                           "ProgramArguments": ["/usr/bin/python3", "/opt/x/weighted_router.py", "--daemon"]}, handle)
        with self.assertRaises(installer.InstallError) as caught:
            self.install()
        self.assertIn("com.example.router", str(caught.exception))
        self.assertFalse(self.app.exists())

    def test_unrelated_agents_are_fine(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        with open(str(agents / "com.other.plist"), "wb") as handle:
            plistlib.dump({"Label": "com.other", "ProgramArguments": ["/bin/true"]}, handle)
        (agents / "broken.plist").write_bytes(b"not a plist")
        self.install()

    def test_busy_port_is_reported(self):
        with self.assertRaises(installer.InstallError) as caught:
            self.install(port_free=lambda: False)
        self.assertIn("17654", str(caught.exception))
        self.assertFalse(self.app.exists())

    def test_only_macos(self):
        with self.assertRaises(installer.InstallError):
            self.install(system="Linux")

    def test_missing_clash_is_a_warning_not_a_blocker(self):
        warnings = self.make().preflight()
        self.assertTrue(any("Clash Verge" in item for item in warnings))
        socket_file = self.home / "verge-mihomo.sock"
        socket_file.write_text("", encoding="utf-8")
        warnings = self.make(sockets=[str(socket_file)]).preflight()
        self.assertFalse(any("没在运行" in item for item in warnings))

    def test_uninstall_removes_program_and_agent_but_keeps_logs(self):
        self.install()
        logs = self.home / "Library/Logs/SteadyRoute"
        (logs / "router.log").write_text("x", encoding="utf-8")
        with mock.patch.object(installer, "say"):
            self.make().uninstall()
        self.assertFalse(self.app.exists())
        self.assertFalse((self.home / "Library/LaunchAgents/com.steadyroute.share.plist").exists())
        self.assertTrue((logs / "router.log").exists())
        with mock.patch.object(installer, "say"):
            self.make().uninstall(purge=True)
        self.assertFalse(logs.exists())

    def test_status_report(self):
        with mock.patch.object(installer, "say"):
            self.assertEqual(self.make().report(), 1)
        self.install()
        with mock.patch.object(installer, "say"):
            self.assertEqual(self.make().report(), 0)


if __name__ == "__main__":
    unittest.main()
