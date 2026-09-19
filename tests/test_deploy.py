import hashlib
import importlib.util
import json
import os
import pathlib
import plistlib
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_PATH = PROJECT_DIR / "scripts" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("steadyroute_deploy", str(MODULE_PATH))
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DeploymentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.project = self.root / "repo"
        self.target = self.root / "live"
        self.backups = self.root / "backups"
        self.plist = self.root / "LaunchAgents" / "service.plist"
        self.git_env = os.environ.copy()
        self.git_env.pop("GIT_DIR", None)
        self.git_env.pop("GIT_WORK_TREE", None)
        self.git_env.pop("GIT_INDEX_FILE", None)
        self.project.mkdir()
        (self.project / "VERSION").write_text("0.2.0\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, env=self.git_env)
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "test@example.invalid"], check=True, env=self.git_env)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "SteadyRoute Test"], check=True, env=self.git_env)
        subprocess.run(["git", "-C", str(self.project), "add", "VERSION"], check=True, env=self.git_env)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "test baseline"], check=True, env=self.git_env)
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.project), "rev-parse", "HEAD"], text=True, env=self.git_env
        ).strip()
        self.archive, self.checksum = self.make_release("0.2.0", self.commit, "new release")
        self.seed_live("old release", "0.1.0", "old-commit")
        self.launchctl_log = self.root / "launchctl.log"
        self.launchctl = self.root / "launchctl"
        self.launchctl.write_text(
            "#!/bin/sh\n"
            "printf '%%s\\n' \"$*\" >> \"%s\"\n"
            "if [ \"${1:-}\" = print ]; then echo 'state = running'; fi\n" % (self.launchctl_log,),
            encoding="utf-8",
        )
        self.launchctl.chmod(0o755)
        self.status_file = self.root / "status.json"
        self.status_file.write_text(json.dumps({
            "service": {"status": "running", "updated_at": int(time.time())},
            "groups": [
                {"name": name, "current": "%s-node" % index, "candidates": ["%s-node" % index]}
                for index, name in enumerate(deploy.REQUIRED_GROUPS)
            ],
        }), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def status_url(self):
        return self.status_file.as_uri()

    def seed_live(self, content, version, commit):
        self.target.mkdir(parents=True, exist_ok=True)
        (self.target / "weighted_router.py").write_text("# %s\n" % content, encoding="utf-8")
        (self.target / "dashboard.html").write_text(content, encoding="utf-8")
        (self.target / "VERSION").write_text(version + "\n", encoding="utf-8")
        (self.target / "GIT_COMMIT").write_text(commit + "\n", encoding="utf-8")
        (self.target / "state.json").write_text('{"keep": true}\n', encoding="utf-8")
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_bytes(plistlib.dumps({"Label": deploy.DEFAULT_LABEL, "Old": True}))

    def make_release(self, version, commit, dashboard):
        package_root = self.root / ("steadyroute-%s" % version)
        (package_root / "src").mkdir(parents=True)
        (package_root / "config").mkdir()
        (package_root / "deploy").mkdir()
        (package_root / "src/weighted_router.py").write_text("print('release')\n", encoding="utf-8")
        (package_root / "src/dashboard.html").write_text(dashboard, encoding="utf-8")
        (package_root / "config/groups.yaml").write_text(
            "# test\nprepend:\n  - name: 香港家宽自动备援\n    type: select\n  - name: AI 台湾家宽线路\n    type: select\nappend: []\ndelete: []\n",
            encoding="utf-8",
        )
        (package_root / "deploy/com.nurture.clash-stability-router.plist").write_bytes(
            plistlib.dumps({"Label": deploy.DEFAULT_LABEL})
        )
        (package_root / "VERSION").write_text(version + "\n", encoding="utf-8")
        (package_root / "GIT_COMMIT").write_text(commit + "\n", encoding="utf-8")
        (package_root / "RELEASE.json").write_text(
            json.dumps({"schema_version": 1, "version": version, "commit": commit, "built_at": "2026-09-19T00:00:00Z"}),
            encoding="utf-8",
        )
        files = sorted(path for path in package_root.rglob("*") if path.is_file())
        (package_root / "MANIFEST.sha256").write_text(
            "".join("%s  %s\n" % (file_sha(path), path.relative_to(package_root).as_posix()) for path in files),
            encoding="utf-8",
        )
        archive = self.root / ("steadyroute-%s.zip" % version)
        with zipfile.ZipFile(str(archive), "w") as bundle:
            for path in package_root.rglob("*"):
                bundle.write(str(path), path.relative_to(self.root).as_posix())
        checksum = pathlib.Path(str(archive) + ".sha256")
        checksum.write_text("%s  %s\n" % (file_sha(archive), archive.name), encoding="utf-8")
        shutil.rmtree(str(package_root))
        return archive, checksum

    def command(self, apply=False):
        arguments = [
            "deploy", "--project-dir", str(self.project), "--package", str(self.archive),
            "--checksum", str(self.checksum), "--target-dir", str(self.target),
            "--backup-dir", str(self.backups), "--launch-agent-path", str(self.plist),
            "--launchctl-bin", str(self.launchctl), "--status-url", self.status_url,
            "--health-timeout", "2", "--skip-project-checks",
        ]
        if apply:
            arguments.append("--apply")
        return arguments

    def test_default_dry_run_does_not_modify_target_or_service(self):
        before = deploy.tree_hashes(self.target)
        self.assertEqual(deploy.main(self.command()), 0)
        self.assertEqual(deploy.tree_hashes(self.target), before)
        self.assertFalse(self.launchctl_log.exists())
        self.assertFalse(self.backups.exists())

    def test_apply_creates_complete_backup_and_passes_health_check(self):
        self.assertEqual(deploy.main(self.command(apply=True)), 0)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.2.0")
        self.assertEqual((self.target / "state.json").read_text(), '{"keep": true}\n')
        backup = deploy.newest_backup(self.backups)
        metadata = deploy.validate_backup(backup)
        self.assertEqual(metadata["version"], "0.1.0")
        self.assertEqual(metadata["commit"], "old-commit")
        self.assertEqual(len(metadata["sha256"]), 64)
        self.assertIn("bootstrap", self.launchctl_log.read_text())

    def test_failure_after_atomic_activation_restores_previous_complete_version(self):
        os.environ["STEADYROUTE_TEST_FAIL_AFTER_ACTIVATE"] = "1"
        try:
            self.assertEqual(deploy.main(self.command(apply=True)), 1)
        finally:
            os.environ.pop("STEADYROUTE_TEST_FAIL_AFTER_ACTIVATE", None)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.0")
        self.assertEqual((self.target / "dashboard.html").read_text(), "old release")
        self.assertEqual((self.target / "state.json").read_text(), '{"keep": true}\n')
        failures = list((self.backups / "diagnostics").glob("failed-*"))
        self.assertEqual(len(failures), 1)
        self.assertTrue((failures[0] / "DEPLOYMENT_FAILURE.txt").is_file())

    def test_one_command_rollback_restores_latest_verified_backup(self):
        self.assertEqual(deploy.main(self.command(apply=True)), 0)
        result = deploy.main([
            "rollback", "--target-dir", str(self.target), "--backup-dir", str(self.backups),
            "--launch-agent-path", str(self.plist), "--launchctl-bin", str(self.launchctl),
            "--status-url", self.status_url, "--health-timeout", "2", "--apply",
        ])
        self.assertEqual(result, 0)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.0")
        self.assertEqual((self.target / "dashboard.html").read_text(), "old release")

    def test_dirty_worktree_is_rejected(self):
        (self.project / "VERSION").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(deploy.DeploymentError):
            deploy.ensure_clean_worktree(self.project)


if __name__ == "__main__":
    unittest.main()
