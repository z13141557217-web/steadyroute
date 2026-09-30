"""v0.5.1 unified install: the release package, and the one installer used from a clone, from
the zip, for upgrades and for migrating an older install. launchd and the status API are fakes."""

import importlib.util
import json
import os
import pathlib
import plistlib
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
sys.path.insert(0, str(ROOT / "src" / "steadyroute"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("steadyroute_installer", ROOT / "scripts" / "installer.py")
build_package = load("steadyroute_build_package", ROOT / "scripts" / "build_package.py")
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
OLD_APP = "Library/Application Support/Clash-Verge-Stability-Router"
OLD_LOGS = "Library/Logs/Clash-Verge-Stability-Router"


class FakeLaunchd(object):
    """Records launchctl calls and which agents are loaded."""

    def __init__(self):
        self.calls = []
        self.loaded = set()
        self.bootstrap_code = 0

    @property
    def running(self):
        return any(path.endswith(installer.LABEL + ".plist") for path in self.loaded)

    def run(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["launchctl", "bootstrap"]:
            if self.bootstrap_code == 0 or not argv[-1].endswith(installer.LABEL + ".plist"):
                self.loaded.add(argv[-1])
                return 0, ""
            return self.bootstrap_code, "Bootstrap failed: 5"
        if argv[:2] == ["launchctl", "bootout"]:
            self.loaded.discard(argv[-1])
        return 0, ""


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tree, cls.archive = build_package.build(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def names(self):
        return {path.relative_to(self.tree).as_posix() for path in self.tree.rglob("*") if path.is_file()}

    def test_package_has_the_repository_layout(self):
        names = self.names()
        for required in ("VERSION", "install.command", "uninstall.command", "使用说明.txt", "scripts/installer.py",
                         "config/route-policies.default.json", "src/steadyroute/weighted_router.py",
                         "src/steadyroute/settings.html", "src/steadyroute/ai_line.py", "src/steadyroute/ai_rules.py"):
            self.assertIn(required, names)
        self.assertEqual(names, {"VERSION", "install.command", "uninstall.command", "使用说明.txt",
                                 "scripts/installer.py", "config/route-policies.default.json"}
                         | {"src/steadyroute/" + name for name in installer.APP_FILES})

    def test_repository_top_level_commands_are_the_packaged_ones(self):
        for name in ("install.command", "uninstall.command"):
            self.assertTrue(os.access(str(ROOT / name), os.X_OK), name)
            self.assertEqual((ROOT / name).read_bytes(), (self.tree / name).read_bytes())
            self.assertIn("scripts/installer.py", (ROOT / name).read_text(encoding="utf-8"))

    def test_package_has_no_personal_data(self):
        self.assertEqual(build_package.check_personal_data(self.tree), [])

    def test_personal_data_check_catches_planted_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.py").write_text('BASE = "/Users/alice/Library"\n', encoding="utf-8")
            (root / "b.json").write_text('{"url": "https://example.com/api/v1/client/subscribe?flag=clash"}',
                                         encoding="utf-8")
            (root / "state.json").write_text("{}", encoding="utf-8")
            (root / "ok.txt").write_text("/Users/Shared is fine", encoding="utf-8")
            reasons = {path: reason for path, reason in build_package.check_personal_data(root)}
            self.assertEqual(set(reasons), {"a.py", "b.json", "state.json"})

    def test_zip_keeps_command_files_executable(self):
        with zipfile.ZipFile(str(self.archive)) as bundle:
            for name in ("install.command", "uninstall.command", "scripts/installer.py"):
                info = bundle.getinfo("SteadyRoute-v%s/%s" % (VERSION, name))
                self.assertTrue((info.external_attr >> 16) & stat.S_IXUSR, name)


class InstallerBase(unittest.TestCase):
    source = ROOT   # a git clone; PackageInstallerTests runs the same flow from the zip tree

    def setUp(self):
        self.home_dir = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.home_dir.name)
        self.launchd = FakeLaunchd()
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
                       port_free=lambda: not self.launchd.loaded, sleep=sleep, python="/usr/bin/python3",
                       sockets=[], clock=lambda: self.clock[0])
        options.update(overrides)
        return installer.Installer(self.source, **options)

    def quiet(self):
        return mock.patch.object(installer, "say", side_effect=lambda message="": self.printed.append(message))

    def install(self, **overrides):
        with self.quiet():
            return self.make(**overrides).install(open_dashboard=False)

    @property
    def app(self):
        return self.home / "Library/Application Support/SteadyRoute"

    @property
    def plist(self):
        return self.home / "Library/LaunchAgents/com.steadyroute.plist"

    def config(self):
        return json.loads((self.app / "config/route-policies.json").read_text(encoding="utf-8"))

    def make_legacy(self, label="com.example.clash-stability-router", env=None):
        """An older install: its own folder, fixed Taiwan / Hong Kong settings, state and logs."""
        old_app = self.home / OLD_APP
        (old_app / "config").mkdir(parents=True)
        (old_app / "weighted_router.py").write_text("# old\n", encoding="utf-8")
        (old_app / "VERSION").write_text("0.5.0\n", encoding="utf-8")
        shutil.copy2(str(FIXTURES / "route-policies.fixed.json"), str(old_app / "config/route-policies.json"))
        (old_app / "state.json").write_text('{"schema_version": 2, "old": true}', encoding="utf-8")
        logs = self.home / OLD_LOGS
        logs.mkdir(parents=True)
        for name in ("router.log", "events.jsonl", "bootstrap.log"):
            (logs / name).write_text(name, encoding="utf-8")
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / (label + ".plist")
        data = {"Label": label, "ProgramArguments": ["/usr/bin/python3", str(old_app / "weighted_router.py"), "--daemon"],
                "RunAtLoad": True, "KeepAlive": True}
        if env:
            data["EnvironmentVariables"] = env
        with open(str(plist), "wb") as handle:
            plistlib.dump(data, handle)
        self.launchd.loaded.add(str(plist))
        return plist


class InstallerTests(InstallerBase):
    def test_fresh_install_places_files_and_a_per_user_launch_agent(self):
        self.install()
        for name in installer.APP_FILES:
            self.assertTrue((self.app / name).is_file(), name)
        self.assertEqual((self.app / "VERSION").read_text(encoding="utf-8").strip(), VERSION)
        with open(str(self.plist), "rb") as handle:
            data = plistlib.load(handle)
        self.assertEqual(data["Label"], "com.steadyroute")
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", str(self.app / "weighted_router.py"), "--daemon"])
        self.assertEqual(data["EnvironmentVariables"]["STEADYROUTE_BASE_DIR"], str(self.app))
        self.assertEqual(data["EnvironmentVariables"]["STEADYROUTE_LOG_DIR"], str(self.home / "Library/Logs/SteadyRoute"))
        self.assertTrue(data["KeepAlive"] and data["RunAtLoad"])
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(self.plist)], self.launchd.calls)
        self.assertEqual(self.config()["profile"], "auto_lock")
        self.assertNotIn("migration", self.config())
        self.assertTrue(any("锁定美国" in line for line in self.printed), self.printed)

    def test_fresh_install_can_point_to_the_ai_line_settings(self):
        asked = []
        with self.quiet():
            self.make(ask=lambda question: asked.append(question) or True).install(open_dashboard=False)
        self.assertTrue(asked and "AI 家宽专线" in asked[0])
        self.assertIn("看板：http://127.0.0.1:17654/settings#ai-line", self.printed)

    def test_upgrade_keeps_state_and_settings_and_backs_up(self):
        self.install()
        (self.app / "state.json").write_text('{"schema_version": 2, "mine": true}', encoding="utf-8")
        (self.app / "clash-applied.json").write_text('{"groups": ["AI 家宽专线"]}', encoding="utf-8")
        config = self.config()
        config["auto_lock"]["exclude_groups"] = ["🎬 流媒体"]
        config["ai_line"] = {"enabled": True, "group_name": "AI 家宽专线", "country": "JP"}
        (self.app / "config/route-policies.json").write_text(json.dumps(config), encoding="utf-8")
        self.clock[0] += 60
        self.install()
        self.assertIn('"mine": true', (self.app / "state.json").read_text(encoding="utf-8"))
        self.assertIn("AI 家宽专线", (self.app / "clash-applied.json").read_text(encoding="utf-8"))
        kept = self.config()
        self.assertEqual(kept["auto_lock"]["exclude_groups"], ["🎬 流媒体"])
        self.assertEqual(kept["ai_line"]["country"], "JP")
        backups = list((self.home / "Library/Application Support/SteadyRoute-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "state.json").exists())

    def test_only_three_backups_are_kept(self):
        for _ in range(6):
            self.install()
            self.clock[0] += 60
        backups_dir = self.home / "Library/Application Support/SteadyRoute-backups"
        self.assertEqual(len(list(backups_dir.iterdir())), 3)

    def test_failed_start_on_upgrade_restores_the_previous_version(self):
        self.install()
        (self.app / "weighted_router.py").write_text("# previous release\n", encoding="utf-8")
        (self.app / "VERSION").write_text("0.5.0\n", encoding="utf-8")
        self.version = "0.5.0"   # the new files never answer with the new version
        with self.assertRaises(installer.InstallError) as caught:
            self.install()
        self.assertIn("已恢复到 v0.5.0", str(caught.exception))
        self.assertEqual((self.app / "weighted_router.py").read_text(encoding="utf-8"), "# previous release\n")
        self.assertEqual(self.launchd.calls[-1][:2], ["launchctl", "bootstrap"], "the old version is started again")

    def test_failed_fresh_install_leaves_nothing_running(self):
        self.launchd.bootstrap_code = 5
        with self.assertRaises(installer.InstallError) as caught:
            self.install()
        self.assertIn("开机自启注册失败", str(caught.exception))
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.launchd.loaded)

    def test_unrelated_agents_are_left_alone(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        with open(str(agents / "com.other.plist"), "wb") as handle:
            plistlib.dump({"Label": "com.other", "ProgramArguments": ["/bin/true"]}, handle)
        (agents / "broken.plist").write_bytes(b"not a plist")
        self.install()
        self.assertTrue((agents / "com.other.plist").exists())
        self.assertFalse(any("com.other" in " ".join(call) for call in self.launchd.calls))

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

    def test_status_report(self):
        with self.quiet():
            self.assertEqual(self.make().report(), 1)
        self.install()
        with self.quiet():
            self.assertEqual(self.make().report(), 0)


class MigrationTests(InstallerBase):
    def test_older_install_is_migrated_and_retired(self):
        old_plist = self.make_legacy()
        self.install()
        self.assertIn('"old": true', (self.app / "state.json").read_text(encoding="utf-8"))
        logs = self.home / "Library/Logs/SteadyRoute"
        self.assertTrue((logs / "router.log").exists() and (logs / "events.jsonl").exists())
        self.assertFalse((logs / "bootstrap.log").exists())
        config = self.config()
        self.assertEqual((config["profile"], config["policies"]), ("auto_lock", []))
        self.assertEqual(config["migration"]["ai_line"],
                         {"enabled": True, "group_name": "AI 台湾家宽线路", "country": "TW"})
        self.assertEqual(config["migration"]["managed_lines"], [{"group_name": "香港家宽自动备援", "country": "HK"}])
        self.assertEqual(sorted(config["migration"]["legacy_group_names"]),
                         ["SteadyRoute 发现·台湾家宽", "SteadyRoute 发现·香港家宽"])
        self.assertEqual(config["auto_lock"]["group_business_urls"]["香港家宽自动备援"], ["https://grok.com/cdn-cgi/trace"])
        self.assertFalse(config.get("ai_line", {}).get("enabled"), "nothing goes into Clash before the user confirms")
        self.assertFalse(old_plist.exists())
        self.assertTrue((self.home / "Library/Application Support/SteadyRoute-backups/legacy" / old_plist.name).exists())
        self.assertNotIn(str(old_plist), self.launchd.loaded)
        self.assertTrue((self.home / OLD_APP / "state.json").exists(), "the old folder is left as it was")
        self.assertIn("看板：http://127.0.0.1:17654/settings", self.printed)
        self.assertTrue(any("迁移旧版" in line for line in self.printed), self.printed)

    def test_failed_migration_starts_the_old_install_again(self):
        old_plist = self.make_legacy()
        self.launchd.bootstrap_code = 5
        with self.assertRaises(installer.InstallError) as caught:
            self.install()
        self.assertIn("旧版已重新启动", str(caught.exception))
        self.assertTrue(old_plist.exists())
        self.assertIn(str(old_plist), self.launchd.loaded)
        self.assertFalse(self.plist.exists())

    def test_legacy_log_folder_comes_from_its_launch_agent(self):
        custom = self.home / "custom-logs"
        custom.mkdir()
        (custom / "router.log").write_text("custom", encoding="utf-8")
        self.make_legacy(env={"STEADYROUTE_LOG_DIR": str(custom)})
        self.install()
        self.assertEqual((self.home / "Library/Logs/SteadyRoute/router.log").read_text(encoding="utf-8"), "custom")

    def test_share_edition_in_the_same_folder_is_upgraded(self):
        self.install()
        config = self.config()
        config["auto_lock"]["exclude_groups"] = ["🎬 流媒体"]
        (self.app / "config/route-policies.json").write_text(json.dumps(config), encoding="utf-8")
        agents = self.home / "Library/LaunchAgents"
        share = agents / "com.steadyroute.share.plist"
        os.replace(str(self.plist), str(share))
        with open(str(share), "rb") as handle:
            data = plistlib.load(handle)
        data["Label"] = "com.steadyroute.share"
        with open(str(share), "wb") as handle:
            plistlib.dump(data, handle)
        self.launchd.loaded = {str(share)}
        self.install()
        self.assertFalse(share.exists())
        self.assertTrue(self.plist.exists())
        self.assertEqual(self.config()["auto_lock"]["exclude_groups"], ["🎬 流媒体"])
        self.assertNotIn("migration", self.config())

    def test_convert_keeps_current_settings_and_ignores_rubbish(self):
        default = json.loads((ROOT / installer.DEFAULT_CONFIG).read_text(encoding="utf-8"))
        current = dict(default, ai_line={"enabled": True, "group_name": "x", "country": "JP"})
        self.assertIs(installer.convert_legacy_config(current, default), current)
        self.assertEqual(installer.convert_legacy_config({"profile": "fixed"}, default), default)
        self.assertEqual(installer.convert_legacy_config(None, default), default)


class UninstallTests(InstallerBase):
    def test_uninstall_removes_program_and_agent_but_keeps_logs(self):
        self.install()
        logs = self.home / "Library/Logs/SteadyRoute"
        (logs / "router.log").write_text("x", encoding="utf-8")
        with self.quiet():
            self.make().uninstall()
        self.assertFalse(self.app.exists())
        self.assertFalse(self.plist.exists())
        self.assertTrue((logs / "router.log").exists())
        with self.quiet():
            self.make().uninstall(purge=True)
        self.assertFalse(logs.exists())

    def test_uninstall_takes_the_lines_out_of_clash_after_stopping(self):
        self.install()
        (self.app / "clash-applied.json").write_text('{"groups": ["AI 家宽专线"]}', encoding="utf-8")
        seen = []

        class Service(object):
            def remove_all(inner):
                seen.append(self.launchd.running)
                return ["AI 家宽专线"]
        with self.quiet():
            self.make(service_factory=Service).uninstall()
        self.assertEqual(seen, [False], "the service is stopped before Clash is changed")
        self.assertIn("已从 Clash 撤销：AI 家宽专线；原有的分组和规则已恢复。", self.printed)
        self.assertFalse(self.app.exists())

    def test_uninstall_keeps_everything_when_clash_cannot_be_restored(self):
        self.install()
        (self.app / "clash-applied.json").write_text('{"groups": ["AI 家宽专线"]}', encoding="utf-8")
        with self.quiet(), self.assertRaises(installer.InstallError) as caught:
            self.make().uninstall()
        self.assertIn("Clash Verge 没有运行", str(caught.exception))
        self.assertTrue(self.app.exists() and self.plist.exists())

        class Broken(object):
            def remove_all(inner):
                raise RuntimeError("核心校验失败")
        with self.quiet(), self.assertRaises(installer.InstallError) as caught:
            self.make(service_factory=Broken).uninstall()
        self.assertIn("核心校验失败", str(caught.exception))
        self.assertTrue(self.app.exists())

    def test_installed_modules_restore_clash(self):
        """The real remove_all of the installed copy, on a Clash Verge folder with our lines in it."""
        import settings_service
        self.install()
        clash_home = self.home / "Library/Application Support" / installer.CLASH_APP_ID
        shutil.copytree(str(FIXTURES / "clash_verge"), str(clash_home))
        before = (clash_home / "profiles/gkX1aa.yaml").read_text(encoding="utf-8")

        class Controller(object):
            def __call__(inner, method, path, payload):
                if path == "/version":
                    return 200, json.dumps({"version": "v1.19.31"})
                if path == "/proxies":
                    names = ["台湾 HiNet 家宽 01 🇨🇳", "AI 台湾家宽线路", "香港家宽自动备援"]
                    return 200, json.dumps({"proxies": {name: {"type": "Hysteria2"} for name in names}})
                return 204, ""

        def factory():
            return settings_service.SettingsService(
                self.app / "config/route-policies.json", self.app, Controller(), clash_home=clash_home,
                core="/bin/true", validator=lambda text: None)
        factory().apply({"ai_line": {"enabled": True, "country": "TW", "group_name": "AI 台湾家宽线路"}})
        self.assertNotEqual((clash_home / "profiles/gkX1aa.yaml").read_text(encoding="utf-8"), before)
        with self.quiet():
            self.make(service_factory=factory).uninstall()
        self.assertEqual((clash_home / "profiles/gkX1aa.yaml").read_text(encoding="utf-8"), before)


class PackageInstallerTests(InstallerBase):
    """The same install from the unpacked zip."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.source, _archive = build_package.build(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_install_and_migrate_from_the_package(self):
        self.make_legacy()
        self.install()
        self.assertEqual((self.app / "VERSION").read_text(encoding="utf-8").strip(), VERSION)
        self.assertIn("migration", self.config())

    def test_packaged_router_loads_the_default_profile(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.dict(os.environ, {"STEADYROUTE_BASE_DIR": base}):
            os.environ.pop("STEADYROUTE_POLICY_CONFIG", None)
            app = self.source / "src" / "steadyroute"
            sys.path.insert(0, str(app))
            try:
                router = load("packaged_router", app / "weighted_router.py")
            finally:
                sys.path.remove(str(app))
            self.assertEqual(router.PROFILE, "auto_lock")
            self.assertTrue(router.POLICY_CONFIG_PATH.endswith("route-policies.default.json"))
            self.assertEqual(router.read_service_version(), VERSION)


if __name__ == "__main__":
    unittest.main()
