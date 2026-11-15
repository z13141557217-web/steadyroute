"""Independent Mihomo, loopback destinations, no real subscription or public requests."""
import collections
import concurrent.futures
import threading
import json
import os
import pathlib
import shutil
import sys
import unittest
from unittest import mock
from urllib.parse import unquote
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness
import geosite_fixture
import real_core
import settings_service
import service_control
import clash_profile
import test_exit_policy_core as kernel
from test_real_core import BASE_CONFIG, GEOSITE


def yaml_config(data):
    rows = []
    for key, value in data.items():
        if isinstance(value, list):
            rows.append(key + ':')
            rows.extend('- ' + json.dumps(v, ensure_ascii=False) for v in value)
        else:
            rows.append(key + ': ' + json.dumps(value, ensure_ascii=False))
    return '\n'.join(rows) + '\n'


@unittest.skipUnless(real_core.available(), 'requires isolated Mihomo')
class NetcoffeeCoreTests(unittest.TestCase):
    def setUp(self):
        lab = kernel.KernelBoundaryTests(methodName='runTest')
        lab.setUp()
        self.addCleanup(lab.tearDown)
        self.lab = lab
        self.home = lab.home / 'verge'
        shutil.copytree(real_core.FIXTURE, self.home)
        for folder in (lab.home, self.home):
            (folder / 'GeoSite.dat').write_bytes(geosite_fixture.geosite_dat(GEOSITE))
        data = json.loads(lab.config())
        data['geox-url'] = {key:'http://127.0.0.1:9/closed-fixture' for key in ('geoip','geosite','mmdb','asn')}
        data['hosts'].update({h:'127.0.0.1' for h in ('2026.ip138.com','my.ip.cn','claude.ai','www.anthropic.com','ordinary.cn')})
        data['rules'] = ['IP-CIDR,1.1.1.1/32,REJECT,no-resolve', 'MATCH,DIRECT']
        self.base_runtime = yaml_config(data)
        self.activate_runtime(self.base_runtime)
        config = dict(BASE_CONFIG, ai_line=lab.line)
        self.config_path = lab.home / 'settings.json'
        self.config_path.write_text(json.dumps(config))
        self.service = settings_service.SettingsService(self.config_path, lab.home / 'app', lab.ctl,
            clash_home=self.home, core=real_core.CORE)
        cycle_harness.authorize_fixture(self.service)
        self.uid_a = self.service.target.resolve()[0]
        self.source(self.uid_a)
        self.service.apply({'netcoffee':True})
        self.requests = collections.Counter()
        r = cycle_harness.load_router('netcoffee_real_probe_router')
        r.SETTINGS = self.service
        r.subscription_authorized = lambda: self.service.scope.allowed()
        r.POLICY_CONFIG_PATH = str(self.config_path)
        r.PROFILE = 'auto_lock'
        r.reload_policy_settings()
        self.r = r

    def source(self, uid):
        (self.home / 'profiles' / (uid + '.yaml')).write_text(json.dumps({'proxies':[
            dict(name=kernel.TARGET,type='http',server='127.0.0.1',port=self.lab.target.server_port),
            dict(name=kernel.BACKUP,type='http',server='127.0.0.1',port=self.lab.backup.server_port)]}))

    def activate_runtime(self, text):
        (self.home / 'clash-verge.yaml').write_text(text)
        self.lab.load(text)

    def probe_api(self, method, path, payload=None, timeout=10):
        if '/delay?' in path:
            name = unquote(path.split('/proxies/',1)[1].split('/delay?',1)[0])
            self.requests[name] += 1
        code, body = self.lab.ctl(method,path,payload)
        if code >= 400:
            raise self.r.ControllerHTTPError(code, body)
        return json.loads(body) if body else None

    def record(self, label, **values):
        self.lab.evidence.append(dict(case=label, **values))

    def test_four_domain_destinations_443_condition_and_domestic_control(self):
        for host in ('2026.ip138.com','my.ip.cn','claude.ai','www.anthropic.com'):
            self.lab.request('/managed-'+host,host=host,chain=kernel.TARGET)
        # The loopback proxy consumes the request, so this never reaches public 1.1.1.1.
        import http.client
        for port, success in ((443,True),(80,False)):
            client=http.client.HTTPConnection('127.0.0.1',self.lab.port,timeout=3)
            try:
                client.request('GET','http://1.1.1.1:%d/port-test'%port)
                response=client.getresponse()
                if success:
                    self.assertEqual(response.read(),b'target')
                else:
                    self.assertGreaterEqual(response.status,400)
            except (OSError,http.client.HTTPException):
                if success:raise
            finally:client.close()
        self.lab.request('/domestic',host='ordinary.cn',expected='DIRECT')
        self.record('destinations', domains=4, tcp_443_target=1, tcp_80_rejected=1, domestic_direct=1,
                    public_ip_checks='not_run')

    def test_actual_delay_requests_all_entry_points_and_empty_scope(self):
        r=self.r;node=kernel.TARGET;url='http://ai.invalid:%d/probe'%self.lab.origin.server_port
        group=kernel.GROUP
        r.apply_policies([dict(r.POLICIES[0],group_name=group,static_candidates=[node,kernel.BACKUP])]) if r.POLICIES else None
        r.GROUPS.clear();r.GROUPS[group]=[node,kernel.BACKUP]
        r.BUSINESS_TEST_URLS[group]=[url]
        state=dict(nodes={},groups={group:{}})
        with mock.patch.object(r,'api_request',side_effect=self.probe_api):
            actions = {
                'quick':lambda:r.timed_probe(node,url,1000),
                'full':lambda:r.run_probe_jobs([(('base',n),n,url,1000) for n in (node,kernel.BACKUP)]),
                'business':lambda:r.business_preflight(group,node,state,allow_fresh_skip=False),
                'warmup':lambda:r.run_probe_jobs([(('warm',n),n,url,1000) for n in (node,kernel.BACKUP)]),
                'retest':lambda:r.confirm_current_failure(node,{'local_ok':True},standby=kernel.BACKUP),
                'pre_switch':lambda:r.precheck_target(group,node,set()),
            }
            for label, action in actions.items():
                before=self.requests.copy();action()
                self.assertGreater(self.requests[node]-before[node],0,label)
                self.assertEqual(self.requests[kernel.BACKUP],0,label)
                self.record(label, selected_requests=self.requests[node]-before[node], unselected_requests=0)
            self.service.apply({'probe_nodes':[]});r.reload_policy_settings()
            before=self.requests.copy()
            for action in actions.values():action()
            r.local_network_ok()
            self.assertEqual(self.requests,before)
            self.record('empty',requests=0)

    def test_a_b_a_binding_restart_reconstruction_and_stopped_limit(self):
        service=self.service
        a_runtime=(self.home/'clash-verge.yaml').read_text()
        a_bindings=service.config()['netcoffee']['bindings'].copy()
        profiles=service.target.profiles_yaml
        original=profiles.read_text()
        # A new remote subscription with its own extension files and no saved binding.
        uid='FixtureB'
        b=original.replace('current: '+self.uid_a,'current: '+uid)
        b += '\n- uid: FixtureB\n  type: remote\n  file: FixtureB.yaml\n  option:\n    groups: FixtureGroupsB\n    rules: FixtureRulesB\n- uid: FixtureGroupsB\n  type: groups\n  file: FixtureGroupsB.yaml\n- uid: FixtureRulesB\n  type: rules\n  file: FixtureRulesB.yaml\n'
        for name in ('FixtureGroupsB','FixtureRulesB'):
            (self.home/'profiles'/(name+'.yaml')).write_text('prepend: []\n')
        self.source(uid)
        profiles.write_text(b)
        b_runtime=clash_profile.patch_runtime(self.base_runtime,[],[],{kernel.GROUP})
        self.activate_runtime(b_runtime)
        self.assertFalse(self.r.probe_authorized(kernel.TARGET))
        with mock.patch.object(self.r,'api_request',wraps=self.r.api_request) as api:
            self.r.run_cycle()
            self.r.run_fast_tick()
            self.r.run_probe_jobs([('old-record',kernel.TARGET,'http://ai.invalid/probe',100)])
            api.assert_not_called()
        with self.assertRaises(clash_profile.ProfileError):
            self.r.api_request('PUT','/configs?force=true',{'payload':a_runtime})
        before={p:p.read_bytes() for p in (self.home/'clash-verge.yaml',*service.target.resolve()[1:],self.config_path,service.applied_path)}
        self.assertEqual(service.ensure_subscription(),'skipped')
        self.assertEqual(service.watch(),'skipped')
        self.assertEqual(service.maintenance(),'skipped')
        self.assertEqual(service.config()['probe_nodes'],[])
        self.assertNotIn(kernel.GROUP,self.lab.get('/proxies')['proxies'])
        self.lab.request('/out-of-scope-ai',host='claude.ai',expected='DIRECT')
        self.lab.request('/domestic-out-of-scope',host='ordinary.cn',expected='DIRECT')
        control=service_control.Control(self.lab.home/'control',service)
        with self.assertRaises(clash_profile.ProfileError):control.target_preview('TW',kernel.TARGET)
        self.assertEqual({p:p.read_bytes() for p in before},before)
        restarted=settings_service.SettingsService(self.config_path,service.base_dir,self.lab.ctl,clash_home=self.home,core=real_core.CORE)
        self.assertFalse(restarted.scope.allowed())
        self.assertEqual(restarted.ensure_subscription(),'skipped')
        # A same display name with a new UID cannot inherit authorization.
        profiles.write_text(b.replace('type: remote','name: 机场订阅\n  type: remote'))
        self.assertFalse(service.scope.allowed())
        self.record('B-out-of-scope',requests=0,writes=0,injected_groups=0,reject_added=False,same_name_reimport='skipped',restart='skipped')
        profiles.write_text(original)
        self.activate_runtime(a_runtime)
        service.ensure_subscription()
        self.assertEqual(service.config()['netcoffee']['bindings'][self.uid_a],a_bindings[self.uid_a])
        self.lab.request('/return-A',host='www.anthropic.com',chain=kernel.TARGET)
        def prepend_items(path):
            lines=path.read_text().splitlines()
            first=next(i for i,line in enumerate(lines) if clash_profile.MARK_BEGIN in line)
            last=next(i for i,line in enumerate(lines) if clash_profile.MARK_END in line)
            return [line.strip()[2:] for line in lines[first+1:last] if line.strip().startswith('- ')]
        _,group_file,rule_file=service.target.resolve()
        composed=clash_profile.patch_runtime(self.base_runtime,prepend_items(group_file),prepend_items(rule_file),{kernel.GROUP})
        self.lab.load(composed)
        self.lab.request('/stopped-persistent-A',host='2026.ip138.com',chain=kernel.TARGET)

        self.assertIn('不能视为始终生效',service.snapshot()['netcoffee']['stopped_limit'])
        self.record('A-B-A',A_target_restored=True,B_skipped=True,restart=True,persisted_runtime_reload=True,
                    extension_composition_fixture=True,native_Clash_Verge_switch='not_tested',unprepared_subscription_guarantee=False)
