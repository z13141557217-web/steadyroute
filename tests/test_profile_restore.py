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
        cycle_harness.authorize_fixture(self.service)
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
    def test_other_subscription_skips_all_automatic_writes_and_replay(self):
        ledger=self.service.applied()
        ledger.setdefault('profiles',{})['new-profile']=dict(groups=[self.group,'香港家宽自动备援'],
            rules=['DOMAIN,claude.ai,'+self.group],removed_items=['- name: Old fixture group'])
        self.service.applied_path.write_text(json.dumps(ledger))
        self.switch_new()
        before = {p:p.read_bytes() for p in (self.service.target.runtime,*self.service.target.resolve()[1:],self.service.applied_path)}
        self.assertEqual(self.service.ensure_subscription(), 'skipped')
        self.assertEqual(self.service.watch(), 'skipped')
        self.assertEqual(self.service.maintenance(), 'skipped')
        self.assertNotIn(self.group,self.core.groups())
        self.assertNotIn('香港家宽自动备援',self.core.groups())
        for action in (lambda:self.service.apply({'ai_line':{'enabled':False}}),
                       lambda:self.service.preview({'netcoffee':True}),
                       lambda:self.service.manager(self.service.config()).disable(),
                       self.service.sync_now):
            with self.assertRaises(clash_profile.ProfileError):action()
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_return_authorized_profile_retains_its_own_originals(self):
        ledger=self.service.applied()
        self.switch_new()
        self.assertEqual(self.service.applied(),ledger)
        self.service.target.profiles_yaml.write_text(self.old_profiles)
        self.service.target.runtime.write_text(self.old_runtime)
        self.assertEqual(self.core.load(self.old_runtime)[0],204)
        self.service.reapply(self.service.config(),self.service.applied(),'returned')
        self.service.manager(self.service.config()).disable()
        self.assertEqual(self.core.groups(),self.original_groups)
        self.assertEqual(self.core.rules(),self.original_rules)

    def test_identity_switch_during_validation_blocks_commit(self):
        writer=self.service.writer()
        plan=self.service.manager(self.service.config()).plan()
        writer.validate=lambda text:self.switch_new()
        with self.assertRaises(clash_profile.ProfileError):
            writer.commit(plan['texts']['groups'],plan['texts']['rules'],plan['texts']['runtime'],{'profile_uid':self.service.target.resolve()[0]})
        self.assertNotIn(self.group,self.core.groups())
        self.assertIn('Native US',self.core.groups())
        self.assertNotIn(self.target,self.service.target.runtime.read_text())

    def test_same_name_reimport_with_different_uid_and_restart_remains_skipped(self):
        self.switch_new()
        self.service.target.profiles_yaml.write_text(self.service.target.profiles_yaml.read_text().replace('type: remote','name: 机场订阅\n  type: remote'))
        restarted=settings_service.SettingsService(self.config_path,self.base,self.core,clash_home=self.core.verge,core=real_core.CORE)
        self.assertFalse(restarted.scope.allowed())
        self.assertEqual(restarted.ensure_subscription(),'skipped')
        self.assertNotIn(self.group,self.core.groups())

    def test_identity_switch_between_writes_never_reloads_old_runtime(self):
        group_file=self.service.target.resolve()[1]
        original=group_file.read_bytes()
        atomic=clash_profile._atomic_write
        switched=[False]
        def write(path,text):
            atomic(path,text)
            if pathlib.Path(path)==group_file and not switched[0]:
                switched[0]=True
                self.switch_new()
        with mock.patch.object(clash_profile,'_atomic_write',side_effect=write):
            with self.assertRaisesRegex(clash_profile.ProfileError,'未向当前订阅重载旧配置'):
                self.service.apply({'manual':['extra.fixture.invalid']})
        self.assertEqual(group_file.read_bytes(),original)
        self.assertNotIn(self.group,self.core.groups())
        self.assertIn('Native US',self.core.groups())
        self.assertNotIn(self.target,self.service.target.runtime.read_text())

    def test_preview_then_switch_never_writes_new_subscription(self):
        self.service.preview({'ai_line':{'enabled':False}})
        self.switch_new()
        before=self.service.target.runtime.read_bytes()
        with self.assertRaises(clash_profile.ProfileError):self.service.apply({'ai_line':{'enabled':False}})
        self.assertEqual(self.service.target.runtime.read_bytes(),before)
