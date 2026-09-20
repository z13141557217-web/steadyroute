import pathlib
import json
import sys
import tempfile
import unittest
import subprocess


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
FIXTURE_DIR = PROJECT_DIR / "tests" / "fixtures"
sys.path.insert(0, str(MODULE_DIR))

import clash_group_deploy
import route_policy


class ClashGroupEnhancementManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        self.profiles_yaml = self.root / "profiles.yaml"
        self.binding = "group-binding-123"
        self.target = self.profiles / (self.binding + ".yaml")
        self.original = b"prepend:\n  - name: old group\nappend: []\ndelete: []\n"
        self.target.write_bytes(self.original)
        self.profiles_yaml.write_text(
            "current: subscription-abc\n"
            "items:\n"
            "  - uid: subscription-other\n"
            "    type: remote\n"
            "    option:\n"
            "      groups: other-binding\n"
            "  - uid: subscription-abc\n"
            "    type: remote\n"
            "    option:\n"
            "      groups: group-binding-123\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def manager(self, **overrides):
        arguments = {
            "profiles_yaml": self.profiles_yaml,
            "profile_dir": self.profiles,
            "policy_path": PROJECT_DIR / "config" / "route-policies.json",
            "generated_groups_path": PROJECT_DIR / "config" / "clash-verge" / "groups.yaml",
            "staged_validator": lambda _rendered: None,
        }
        arguments.update(overrides)
        return clash_group_deploy.ClashGroupEnhancementManager(**arguments)

    def test_plan_resolves_current_subscription_group_binding_without_writes(self):
        plan = self.manager().plan()
        self.assertEqual(plan["current_profile_uid"], "subscription-abc")
        self.assertEqual(plan["binding_uid"], self.binding)
        self.assertEqual(plan["target"], str(self.target))
        self.assertEqual(plan["mode"], "shadow")
        self.assertEqual(plan["action"], "replace")
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse((self.profiles / ".steadyroute-group-backups").exists())

    def test_plan_ignores_real_shape_nested_selected_and_other_lists(self):
        self.profiles_yaml.write_text(
            (FIXTURE_DIR / "profiles_nested_selected.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        plan = self.manager().plan()
        self.assertEqual(plan["current_profile_uid"], "current-remote")
        self.assertEqual(plan["binding_uid"], "group-binding-123")
        self.assertEqual(plan["target"], str(self.target))
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_profile_parser_rejects_duplicate_ambiguous_or_complex_structures(self):
        valid = (FIXTURE_DIR / "profiles_nested_selected.yaml").read_text(encoding="utf-8")
        cases = {
            "duplicate current": "current: second-current\n" + valid,
            "duplicate items block": valid + "\nitems:\n- uid: extra\n  type: remote\n",
            "duplicate item uid": valid + "\n- uid: current-remote\n  type: remote\n",
            "duplicate direct uid": valid.replace(
                "  type: remote\n  name: Sanitized Remote",
                "  type: remote\n  uid: second-uid\n  name: Sanitized Remote",
            ),
            "duplicate option groups": valid.replace(
                "    groups: group-binding-123",
                "    groups: group-binding-123\n    groups: second-binding",
            ),
            "duplicate option mapping": valid.replace(
                "  option:\n", "  option:\n  option:\n", 1
            ),
            "odd indentation": valid.replace("  type: remote", "   type: remote", 1),
            "inline items flow": "current: current-remote\nitems: [{uid: current-remote, type: remote}]\n",
            "complex option flow": valid.replace(
                "  option:\n", "  option: {groups: group-binding-123}\n", 1
            ),
            "top item without uid": valid.replace("- uid: local-profile", "- name: local-profile", 1),
        }
        for label, source in cases.items():
            with self.subTest(case=label):
                self.profiles_yaml.write_text(source, encoding="utf-8")
                with self.assertRaises(clash_group_deploy.GroupEnhancementError):
                    self.manager().plan()

    def test_plan_rejects_missing_unsafe_or_unvalidated_bindings(self):
        cases = []

        missing_current = self.profiles_yaml.read_text(encoding="utf-8").replace(
            "current: subscription-abc", "current: missing-subscription"
        )
        cases.append(("missing current binding", {"profiles_text": missing_current}))

        traversal = self.profiles_yaml.read_text(encoding="utf-8").replace(
            "groups: group-binding-123", "groups: ../outside"
        )
        cases.append(("traversal binding", {"profiles_text": traversal}))

        non_subscription = self.profiles_yaml.read_text(encoding="utf-8").replace(
            "  - uid: subscription-abc\n    type: remote",
            "  - uid: subscription-abc\n    type: local",
        )
        cases.append(("current is not a remote subscription", {"profiles_text": non_subscription}))

        cases.append(("broad directory", {"profile_dir": self.root}))
        cases.append(("failed staged validation", {"validator": lambda _value: (_ for _ in ()).throw(RuntimeError("bad config"))}))

        for label, setup in cases:
            with self.subTest(case=label):
                original_text = self.profiles_yaml.read_text(encoding="utf-8")
                if "profiles_text" in setup:
                    self.profiles_yaml.write_text(setup["profiles_text"], encoding="utf-8")
                try:
                    manager = self.manager(
                        profile_dir=setup.get("profile_dir", self.profiles),
                        staged_validator=setup.get("validator", lambda _value: None),
                    )
                    with self.assertRaises((clash_group_deploy.GroupEnhancementError, RuntimeError)):
                        manager.plan()
                finally:
                    self.profiles_yaml.write_text(original_text, encoding="utf-8")
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertFalse((self.profiles / ".steadyroute-group-backups").exists())

        source = PROJECT_DIR / "config" / "clash-verge" / "groups.yaml"
        drifted = self.root / "drifted.yaml"
        drifted.write_bytes(source.read_bytes() + b"# drift\n")
        with self.assertRaises(clash_group_deploy.GroupEnhancementError):
            self.manager(generated_groups_path=drifted).plan()

        self.target.unlink()
        outside = self.root / "outside.yaml"
        outside.write_bytes(self.original)
        self.target.symlink_to(outside)
        with self.assertRaises(clash_group_deploy.GroupEnhancementError):
            self.manager().plan()

    def test_apply_is_explicit_and_creates_recoverable_backup_before_atomic_replace(self):
        manager = self.manager()
        dry_run = manager.apply(apply=False)
        self.assertEqual(dry_run["result"], "dry-run")
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse((self.profiles / ".steadyroute-group-backups").exists())

        result = manager.apply(apply=True)
        expected = (PROJECT_DIR / "config" / "clash-verge" / "groups.yaml").read_bytes()
        self.assertEqual(result["result"], "applied")
        self.assertEqual(self.target.read_bytes(), expected)
        backup = pathlib.Path(result["backup"])
        self.assertEqual((backup / "original.yaml").read_bytes(), self.original)
        metadata = json.loads((backup / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["target"], self.target.name)
        self.assertEqual(metadata["original_sha256"], clash_group_deploy.sha256_bytes(self.original))
        self.assertEqual(metadata["replacement_sha256"], clash_group_deploy.sha256_bytes(expected))
        self.assertEqual(metadata["mode"], "shadow")

    def test_failure_after_replace_automatically_restores_original(self):
        def fail_after_write(_target, _expected_sha):
            raise RuntimeError("injected post-write failure")

        manager = self.manager(post_write_validator=fail_after_write)
        with self.assertRaises(clash_group_deploy.GroupEnhancementError):
            manager.apply(apply=True)
        self.assertEqual(self.target.read_bytes(), self.original)
        backups = list((self.profiles / ".steadyroute-group-backups").glob("*/metadata.json"))
        self.assertEqual(len(backups), 1)

    def test_rollback_is_dry_run_by_default_and_restores_verified_backup(self):
        manager = self.manager()
        applied = manager.apply(apply=True)
        replacement = self.target.read_bytes()
        backup = pathlib.Path(applied["backup"])

        dry_run = manager.rollback(backup, apply=False)
        self.assertEqual(dry_run["result"], "dry-run")
        self.assertEqual(self.target.read_bytes(), replacement)

        result = manager.rollback(backup, apply=True)
        self.assertEqual(result["result"], "rolled-back")
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_rollback_rejects_unrelated_backup_and_changed_current_target(self):
        manager = self.manager()
        applied = manager.apply(apply=True)
        backup = pathlib.Path(applied["backup"])
        unrelated = self.root / "unrelated-backup"
        unrelated.mkdir()
        with self.assertRaises(clash_group_deploy.GroupEnhancementError):
            manager.rollback(unrelated, apply=True)

        self.target.write_text("manually changed\n", encoding="utf-8")
        with self.assertRaises(clash_group_deploy.GroupEnhancementError):
            manager.rollback(backup, apply=True)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "manually changed\n")

    def test_rollback_remains_available_when_generated_source_becomes_invalid(self):
        generated = self.root / "generated-groups.yaml"
        generated.write_bytes((PROJECT_DIR / "config" / "clash-verge" / "groups.yaml").read_bytes())
        manager = self.manager(generated_groups_path=generated)
        applied = manager.apply(apply=True)
        generated.write_text("invalid drift\n", encoding="utf-8")
        result = manager.rollback(pathlib.Path(applied["backup"]), apply=True)
        self.assertEqual(result["result"], "rolled-back")
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_cli_deploy_defaults_to_dry_run_with_explicit_injected_paths(self):
        core = self.root / "fake-mihomo"
        core.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        core.chmod(0o700)
        result = subprocess.run(
            [
                sys.executable, str(PROJECT_DIR / "scripts" / "manage-clash-groups.py"),
                "deploy", "--profiles-yaml", str(self.profiles_yaml),
                "--profile-dir", str(self.profiles), "--core", str(core),
            ],
            cwd=str(PROJECT_DIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["result"], "dry-run")
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse((self.profiles / ".steadyroute-group-backups").exists())

    def test_discovery_verification_requires_every_configured_group_in_proxies_payload(self):
        config = route_policy.load_policy_config(PROJECT_DIR / "config" / "route-policies.json")
        payload = {"proxies": {}}
        for policy in config["policies"]:
            payload["proxies"][policy["discovery_group_name"]] = {
                "all": list(policy["static_candidates"]), "now": policy["static_candidates"][0],
            }
        result = clash_group_deploy.verify_discovery_payload(config, payload)
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["groups"]), 2)

        del payload["proxies"][config["policies"][0]["discovery_group_name"]]
        with self.assertRaises(clash_group_deploy.GroupEnhancementError):
            clash_group_deploy.verify_discovery_payload(config, payload)


if __name__ == "__main__":
    unittest.main()
