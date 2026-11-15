"""Cross-subscription rollback uses a real isolated Mihomo."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness
import clash_profile
import real_core
import settings_service
from test_real_core import BASE_CONFIG, GEOSITE

@unittest.skipUnless(real_core.available(), 'requires isolated Mihomo')
class SubscriptionProfilesTests(unittest.TestCase):
    def setUp(self):
        self.core = real_core.Core(GEOSITE)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        (self.base / 'config').mkdir()
        self.config_path = self.base / 'config/route-policies.json'
        config = dict(BASE_CONFIG, managed_lines=[{'group_name': '香港家宽自动备援', 'country': 'HK'}])
        self.config_path.write_text(json.dumps(config))
        self.service = settings_service.SettingsService(self.config_path, self.base, self.core,
            clash_home=self.core.verge, core=real_core.CORE, fetch=lambda url: b'')
        self.old_profiles = self.service.target.profiles_yaml.read_text()
        self.group = 'AI 台湾家宽线路'
        self.target = self.core.proxies()[self.group]['now']
        self.original_groups, self.original_rules = self.core.groups(), self.core.rules()
        self.service.apply({'ai_line': {'enabled': True, 'group_name': self.group, 'country': 'TW',
            'exit_policy': 'locked', 'locked_node': self.target}})
        self.old_runtime = self.service.target.runtime.read_text()
    def tearDown(self):
        self.core.stop()
        self.tmp.cleanup()
    def switch_new(self):
        home = self.core.verge
        for name in ('new-groups', 'new-rules'):
            (home / 'profiles' / (name + '.yaml')).write_text('prepend: []\nappend: []\ndelete: []\n')
        self.service.target.profiles_yaml.write_text('current: new-profile\nitems:\n- uid: new-profile\n  type: remote\n  option:\n    groups: new-groups\n    rules: new-rules\n')
        text = ('proxies:\n- {"name":"US ISP new", "type":"http", "server":"127.0.0.1", "port":9}\n'
                'proxy-groups:\n- {"name":"Native US", "type":"select", "proxies":["US ISP new"]}\n'
                'rules:\n- MATCH,REJECT\n' + real_core.GEO)
        self.service.target.runtime.write_text(text)
        self.assertEqual(self.core.load(text)[0], 204)
        self.service.reapply(self.service.config(), self.service.applied(), 'subscription switched')
    def test_partial_disable_never_restores_old_static_nodes_into_new_subscription(self):
        self.switch_new()
        self.assertEqual(self.core.proxies()[self.group]['all'], ['REJECT'])
        self.service.apply({'ai_line': {'enabled': False}})
        self.assertNotIn(self.group, self.core.groups())
        self.assertEqual(self.core.proxies()['Native US']['all'], ['US ISP new'])
        self.assertNotIn(self.target, self.service.target.runtime.read_text())
    def test_return_to_original_profile_restores_its_own_groups_and_rules(self):
        self.switch_new()
        self.service.target.profiles_yaml.write_text(self.old_profiles)
        self.service.target.runtime.write_text(self.old_runtime)
        self.assertEqual(self.core.load(self.old_runtime)[0], 204)
        self.assertIn('当前订阅', self.service.line_problems(self.service.applied()))
        self.assertIsNone(self.service.snapshot()['exit_policy']['effective_mode'])
        self.service.reapply(self.service.config(), self.service.applied(), 'returned')
        self.service.manager(self.service.config()).disable()
        self.assertEqual(self.core.groups(), self.original_groups)
        self.assertEqual(self.core.rules(), self.original_rules)
    def test_persistence_failure_rolls_back_new_profile_and_preserves_old_ledger(self):
        self.switch_new()
        before = self.service.applied()
        runtime = self.service.target.runtime.read_text()
        with mock.patch.object(self.service, 'save_config', side_effect=OSError('storage failure')):
            with self.assertRaisesRegex(clash_profile.ProfileError, '已回滚'):
                self.service.apply({'ai_line': {'enabled': False}})
        self.assertEqual(self.service.applied(), before)
        self.assertEqual(self.service.target.runtime.read_text(), runtime)
        self.assertEqual(self.core.proxies()[self.group]['all'], ['REJECT'])

    def test_disable_new_profile_keeps_original_subscription_restore_record(self):
        self.switch_new()
        result = self.service.manager(self.service.config()).disable()
        self.service.applied_path.write_text(json.dumps(result))
        self.assertNotIn(self.group, self.core.groups())
        self.assertTrue(result.get("profiles"))
        self.service.target.profiles_yaml.write_text(self.old_profiles)
        self.service.target.runtime.write_text(self.old_runtime)
        self.assertEqual(self.core.load(self.old_runtime)[0], 204)
        self.service.reapply(self.service.config(), self.service.applied(), 'returned after disable')
        self.service.manager(self.service.config()).disable()
        self.assertEqual(self.core.groups(), self.original_groups)
        self.assertEqual(self.core.rules(), self.original_rules)

    def test_preview_owner_change_is_refused_before_any_profile_write(self):
        old_uid = self.service.target.resolve()[0]
        self.switch_new()
        target = self.service.target
        before = {p: p.read_bytes() for p in [target.runtime, *target.resolve()[1:]]}
        with self.assertRaisesRegex(clash_profile.ProfileError, '订阅已变化'):
            self.service.writer().commit('prepend: []', 'prepend: []', target.runtime.read_text(),
                                         {"profile_uid": old_uid})
        self.assertEqual({p: p.read_bytes() for p in before}, before)
