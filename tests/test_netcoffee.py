"""Synthetic fixtures only: import ownership and explicit probe authorization."""
import concurrent.futures
import json
import pathlib
import sys
import threading
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness
from test_settings import ServiceBase, NODES
import clash_profile


class ImportTests(ServiceBase):
    def setUp(self):
        super().setUp()
        config = json.loads(self.config_path.read_text())
        config['ai_line'] = dict(enabled=True, country='TW', group_name='AI 台湾家宽线路',
                                 exit_policy='locked', locked_node=NODES[0])
        self.config_path.write_text(json.dumps(config))
        original = self.clash.proxies
        self.clash.proxies = lambda: dict(original(), **{'AI 台湾家宽线路':
            dict(type='Selector', now=NODES[0], all=[NODES[0]])})
        self.clash.live_rules = True
        uid = self.service.target.resolve()[0]
        (self.clash_home / 'profiles' / (uid + '.yaml')).write_text(json.dumps({'proxies':[
            dict(name=n, type='http', server='127.0.0.1', port=9) for n in NODES]}))

    def test_preview_import_retry_and_revoke_preserve_existing_claude(self):
        before = self.files()
        plan = self.service.preview({'netcoffee': True})
        self.assertEqual(self.files(), before)
        self.assertEqual(len(plan['netcoffee']['conditions']), 5)
        self.assertEqual(len(plan['netcoffee']['added_rules']), 3)
        self.service.apply({'netcoffee': True})
        after = self.files()
        self.service.apply({'netcoffee': True})
        self.assertEqual(self.files(), after)
        self.assertEqual(sum(r['payload']=='claude.ai' for r in self.clash.loaded.rules()), 1)
        self.assertEqual(self.service.snapshot()['netcoffee']['ip_checks'][0]['status'], 'unverified')
        self.service.apply({'netcoffee': False})
        self.assertNotIn('2026.ip138.com', self.files()['clash-verge.yaml'])
        self.assertIn('claude.ai', self.files()['clash-verge.yaml'])

    def test_missing_or_drifted_group_blocks_without_write(self):
        for data in ({}, {NODES[0]: {'type':'Http'}, 'AI 台湾家宽线路':
                      dict(type='Selector', now=NODES[1], all=NODES[:2])}):
            with mock.patch.object(self.service, 'proxies', return_value=data):
                before = self.files()
                with self.assertRaises(clash_profile.ProfileError):
                    self.service.apply({'netcoffee': True})
                self.assertEqual(self.files(), before)

    def test_reload_failure_rolls_back_then_retry_succeeds(self):
        before = self.files()
        self.clash.refuse_reload=True
        with self.assertRaises(clash_profile.ProfileError):
            self.service.apply({'netcoffee': True})
        self.assertEqual(self.files(), before)
        self.assertFalse(self.service.config().get('netcoffee'))
        self.clash.refuse_reload=False
        self.service.apply({'netcoffee': True})

    def test_probe_selection_deduplicates_and_saves_without_clash_reload(self):
        before = self.files()
        self.service.apply({'probe_nodes': [NODES[0], NODES[0]]})
        self.assertEqual(self.service.config()['probe_nodes'], [NODES[0]])
        self.assertEqual(self.files(), before)
        self.service.apply({'probe_nodes': []})
        self.assertEqual(self.service.snapshot()['probe_nodes'], [])


class ProbeConsentTests(unittest.TestCase):
    def setUp(self):
        self.r = cycle_harness.load_router('consent_router')
        self.a, self.b = list(self.r.GROUPS.values())[0][:2]
        self.r.POLICY_CONFIG['probe_nodes'] = [self.a]

    def test_all_entry_points_check_actual_request_boundary(self):
        r=self.r
        calls=[]
        with mock.patch.object(r,'api_request',side_effect=lambda method,path,**kw: calls.append(path) or {'delay':12}):
            for name in (self.a,self.b):
                r.probe_node(name)
                r.timed_probe(name,r.FAST_PROBE_URL,100)
                r.business_preflight(next(iter(r.GROUPS)),name,{'nodes':{},'groups':{}},allow_fresh_skip=False)
                r.precheck_target(next(iter(r.GROUPS)),name,set())
            self.assertGreater(len(calls),0)
            from urllib.parse import unquote
            self.assertTrue(all(self.b not in unquote(p) for p in calls))
            calls.clear()
            r.POLICY_CONFIG['probe_nodes']=[]
            r.local_network_ok()
            r.run_probe_jobs([('a',self.a,r.TEST_URL,100)])
            self.assertEqual(calls,[])

    def test_queued_job_cancelled_and_duplicates_share_one_request(self):
        r=self.r
        pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        hold=threading.Event(); started=threading.Event()
        pool.submit(lambda:(started.set(),hold.wait(5)))
        started.wait(1)
        with mock.patch.object(r,'probe_pool',return_value=pool), mock.patch.object(r,'api_request',return_value={'delay':12}) as api:
            with concurrent.futures.ThreadPoolExecutor(1) as caller:
                result=caller.submit(r.run_probe_jobs,[('one',self.a,r.TEST_URL,100),('two',self.a,r.TEST_URL,100)])
                for _ in range(1000):
                    if pool._work_queue.qsize(): break
                    threading.Event().wait(.001)
                r.POLICY_CONFIG['probe_nodes']=[]
                hold.set(); self.assertEqual(result.result(5),{})
                api.assert_not_called()
            r.POLICY_CONFIG['probe_nodes']=[self.a]
            result=r.run_probe_jobs([('one',self.a,r.TEST_URL,100),('two',self.a,r.TEST_URL,100)])
            self.assertEqual(result,{'one':12,'two':12});self.assertEqual(api.call_count,1)
        pool.shutdown()


class SubscriptionScopeTests(ServiceBase):
    def test_missing_or_corrupt_local_authorization_never_adopts_current_subscription(self):
        before=self.files()
        for text in (None, '{}', '{broken', '{"schema_version":1,"authorized_uid":"other-fixture"}'):
            if text is None:self.service.scope.path.unlink(missing_ok=True)
            else:self.service.scope.path.write_text(text)
            self.assertFalse(self.service.scope.allowed())
            self.assertEqual(self.service.ensure_subscription(),'skipped')
            self.assertEqual(self.service.watch(),'skipped')
            self.assertEqual(self.service.maintenance(),'skipped')
            with self.assertRaises(clash_profile.ProfileError):self.service.apply({'netcoffee':True})
            self.assertFalse(self.service.snapshot()['subscription_scope']['active'])
            self.assertEqual(self.files(),before)

    def test_scope_revocation_cancels_jobs_waiting_in_executor(self):
        r=cycle_harness.load_router('scoped_queue_router')
        r.SETTINGS=self.service
        r.subscription_authorized=lambda:self.service.scope.allowed()
        r.POLICY_CONFIG['probe_nodes']=[NODES[0]]
        hold=threading.Event();ready=threading.Event()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pool.submit(lambda:(ready.set(),hold.wait(5)));ready.wait(1)
            with mock.patch.object(r,'probe_pool',return_value=pool), mock.patch.object(r,'api_request') as api:
                with concurrent.futures.ThreadPoolExecutor(1) as caller:
                    future=caller.submit(r.run_probe_jobs,[('queued',NODES[0],r.TEST_URL,100)])
                    for _ in range(1000):
                        if pool._work_queue.qsize():break
                        threading.Event().wait(.001)
                    self.service.scope.path.unlink()
                    hold.set();self.assertEqual(future.result(5),{})
                    api.assert_not_called()
