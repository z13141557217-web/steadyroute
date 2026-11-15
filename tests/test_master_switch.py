"""Lifecycle barriers and user-visible stopped state; no production files."""
import json
import http.client
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src/steadyroute'))
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import operation_gate
import service_control
import settings_service
import service_cli
from test_settings import ServiceBase, NODES


class BarrierTests(unittest.TestCase):
    def test_stop_drains_inflight_transaction_and_rejects_late_callbacks(self):
        gate = operation_gate.Gate()
        entered, finish, stopped = threading.Event(), threading.Event(), threading.Event()
        writes = []
        def transaction():
            with gate.operation():
                entered.set()
                finish.wait(3)
                with gate.operation():
                    writes.append('committed')
        task = threading.Thread(target=transaction)
        task.start()
        self.assertTrue(entered.wait(1))
        closer = threading.Thread(target=lambda: (gate.close(3), stopped.set()))
        closer.start()
        time.sleep(.03)
        self.assertFalse(stopped.is_set())
        with self.assertRaises(operation_gate.Stopped):
            with gate.operation():
                writes.append('late')
        finish.set()
        task.join(2); closer.join(2)
        self.assertTrue(stopped.is_set())
        self.assertEqual(writes, ['committed'])
        with self.assertRaises(operation_gate.Stopped):
            with gate.operation():
                writes.append('late callback')

    def test_timeout_never_reports_drained_or_reopens_admission(self):
        gate = operation_gate.Gate()
        with gate.operation():
            with self.assertRaises(TimeoutError):
                # Closing from an active operation cannot drain itself.
                gate.close(.01)
        self.assertFalse(gate.accepting)
        gate.close(.1)


class LifecycleTests(ServiceBase):
    def control(self):
        return service_control.Control(self.base, self.service, spawn=mock.Mock())

    def test_preview_does_not_write_and_source_missing_blocks_start(self):
        c = self.control()
        before = self.files()
        p = c.preview('start')
        self.assertEqual(self.files(), before)
        self.assertIn('fingerprint', p)
        config = self.service.config()
        config['ai_line'] = dict(enabled=True, country='TW', group_name='AI 台湾家宽线路',
                                 exit_policy='locked', locked_node=NODES[0])
        self.service.save_config(config)
        # Fixture has no source subscription file: refuse rather than trust stale runtime.
        with self.assertRaisesRegex(Exception, '订阅源|订阅文件'):
            c.preview('start')

    def test_explicit_off_ignores_lock_drift_but_target_update_still_refused(self):
        config = self.service.config()
        config['ai_line'] = dict(enabled=True, country='TW', group_name='AI 台湾家宽线路', exit_policy='locked', locked_node=NODES[0])
        self.service.save_config(config)
        self.clash.loaded.select('AI 台湾家宽线路', NODES[1]) if hasattr(self.clash.loaded, 'select') else None
        with mock.patch.object(self.service, 'proxies', return_value={
                NODES[0]: {'type':'Hysteria2'}, NODES[1]: {'type':'Hysteria2'},
                'AI 台湾家宽线路': {'type':'Selector','now':NODES[1], 'all':NODES[:2]}}):
            self.assertFalse(self.service.merged({'ai_line':{'enabled':False}})['ai_line']['enabled'])
            with self.assertRaisesRegex(Exception, '当前选择'):
                self.service.merged({'ai_line':{'locked_node':'台湾 HiNet 家宽 11'}})

    def test_login_setting_and_pause_are_independent_and_persistent(self):
        c = self.control()
        c.set_login(False)
        self.assertFalse(self.control().state['login_start'])
        c.pause()
        self.assertFalse(c.state['desired_running'])
        c.set_login(True)
        self.assertFalse(self.control().state['desired_running'])
        self.assertTrue(self.control().state['login_start'])

    def test_stale_preview_and_unconfirmed_release_are_rejected(self):
        c = self.control()
        p = c.preview('release')
        with self.assertRaisesRegex(Exception, '确认'):
            c.change('release', p['fingerprint'], False)
        self.config_path.write_text(self.config_path.read_text() + '\n')
        with self.assertRaisesRegex(Exception, '变化'):
            c.change('release', p['fingerprint'], True)

    def test_worker_crash_is_failed_and_does_not_auto_restart(self):
        c = self.control()
        c.worker = mock.Mock()
        c.worker.poll.return_value = 1
        c.state['actual'] = 'running'
        s = c.snapshot()
        self.assertEqual(s['actual'], 'failed')
        self.assertFalse(s['desired_running'])
        c.spawn.assert_not_called()

    def test_release_preserves_preferences_and_history_and_is_idempotent(self):
        self.service.apply({'ai_line': {'enabled':True, 'country':'JP'}})
        c = self.control()
        config = self.config_path.read_bytes()
        (self.base/'state.json').write_text('{"history":[1]}')
        c.change('release', c.preview('release')['fingerprint'], True)
        self.assertEqual(c.state['actual'], 'released')
        self.assertEqual(self.config_path.read_bytes(), config)
        self.assertEqual((self.base/'state.json').read_text(), '{"history":[1]}')
        self.assertFalse(self.service.applied().get('groups'))
        calls = list(self.clash.calls)
        c.change('release', c.preview('release')['fingerprint'], True)
        self.assertEqual([x for x in self.clash.calls[len(calls):] if x[0]=='PUT'], [])

    def test_release_failure_keeps_worker_off_and_reports_failed(self):
        self.service.apply({'ai_line': {'enabled':True, 'country':'JP'}})
        c = self.control()
        self.clash.refuse_reload = True
        with self.assertRaises(Exception):
            c.change('release', c.preview('release')['fingerprint'], True)
        self.assertEqual(c.state['actual'], 'failed')
        self.assertFalse(c.state['desired_running'])
        self.assertTrue(self.service.applied().get('groups'))



class ControlFailureTests(ServiceBase):
    def control(self):
        return service_control.Control(self.base,self.service,spawn=mock.Mock())

    def test_false_drain_reply_is_failure_and_never_terminates_transaction(self):
        c=self.control();c.worker=mock.Mock();c.worker.poll.return_value=None
        with mock.patch.object(c,'_worker_call',return_value=(200,'application/json',b'{"drained":false}')):
            with self.assertRaisesRegex(RuntimeError,'停止屏障'):
                c.pause()
        c.worker.terminate.assert_not_called()
        self.assertFalse(c.state['desired_running'])
        self.assertEqual(c.state['actual'],'failed')

    def test_unknown_or_corrupt_control_record_is_never_overwritten(self):
        path=self.base/'control-state.json'
        for content in ('{', '{"schema_version":2}', '{"schema_version":1,"desired_running":"yes","login_start":true,"actual":"paused"}'):
            path.write_text(content)
            with self.assertRaises(Exception):self.control()
            self.assertEqual(path.read_text(),content)

    def test_release_record_failure_rolls_back_kernel_files_and_record(self):
        self.clash.live_rules=True
        self.service.apply({'ai_line':{'enabled':True,'country':'JP'}})
        c=self.control();files=self.files();record=self.service.applied_path.read_bytes()
        original=settings_service._atomic_json
        calls=[]
        def atomic(path,data):
            if pathlib.Path(path)==self.service.applied_path and not calls:
                calls.append(True);raise OSError('test storage full')
            return original(path,data)
        with mock.patch.object(settings_service,'_atomic_json',side_effect=atomic):
            with self.assertRaisesRegex(Exception,'已恢复'):
                c.change('release',c.preview('release')['fingerprint'],True)
        self.assertEqual(self.files(),files)
        self.assertEqual(self.service.applied_path.read_bytes(),record)
        self.assertEqual(self.clash.loaded.text,files['clash-verge.yaml'])
        self.assertEqual(c.state['actual'],'failed')

    def test_ignored_reload_with_same_group_names_does_not_report_release(self):
        self.clash.live_rules=True
        self.service.apply({'ai_line':{'enabled':True,'country':'JP','group_name':'AI 台湾家宽线路'}})
        c=self.control();self.clash.ignore_reload=True
        with self.assertRaisesRegex(Exception,'额外的稳航受管规则'):
            c.change('release',c.preview('release')['fingerprint'],True)
        self.assertNotEqual(c.state['actual'],'released')

    def test_http_stopped_settings_and_foreign_origin_are_rejected(self):
        router=__import__('cycle_harness').load_router('master_http_router')
        c=self.control()
        server=router.QuietHTTPServer(('127.0.0.1',0),service_control.make_handler(c,router))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        port=server.server_address[1]
        def request(route,method='GET',body=None,origin=None):
            client=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            headers={'Host':'127.0.0.1:%d'%port}
            if method=='POST':headers.update({'Origin':origin or 'http://127.0.0.1:%d'%port,'X-SteadyRoute':'1','Content-Type':'application/json'})
            client.request(method,route,body=json.dumps(body) if body is not None else None,headers=headers)
            r=client.getresponse();data=r.read();code=r.status;client.close();return code,data
        try:
            calls=list(self.clash.calls)
            code,data=request('/api/control')
            self.assertEqual(code,200);self.assertFalse(json.loads(data)['worker_running'])
            self.assertEqual(self.clash.calls,calls,'reading control state must not probe or scan core')
            self.assertEqual(request('/api/settings/apply','POST',{'ai_line':{'enabled':True}})[0],409)
            self.assertEqual(request('/api/control/change','POST',{'action':'pause'},'https://untrusted.invalid')[0],403)
            self.assertEqual(request('/api/control/quiesce','POST',{})[0],404)
            self.assertEqual(request('/control')[0],200)
            self.assertIn(b'/assets/control.js',request('/control')[1])
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_lifecycle_final_record_failure_restores_released_rules(self):
        self.clash.live_rules=True
        self.service.apply({'ai_line':{'enabled':True,'country':'JP'}})
        c=self.control();files=self.files();record=self.service.applied_path.read_bytes()
        original=c.save
        def save(**changes):
            if changes.get('actual')=='released':raise OSError('final state full')
            return original(**changes)
        with mock.patch.object(c,'save',side_effect=save):
            with self.assertRaisesRegex(Exception,'final state full'):
                c.change('release',c.preview('release')['fingerprint'],True)
        self.assertEqual(self.files(),files)
        self.assertEqual(self.service.applied_path.read_bytes(),record)
        self.assertEqual(c.state['actual'],'failed')


class IndependentEntryTests(ServiceBase):
    def setUp(self):
        super().setUp()
        self.entry_env = mock.patch.dict(service_cli.os.environ, {'STEADYROUTE_BASE_DIR':str(self.base)})
        self.entry_env.start()
        self.addCleanup(self.entry_env.stop)

    def test_stop_quiesces_before_unloading_control_plane(self):
        order=[]
        class Connection:
            def __init__(self,*a,**k):pass
            def request(self,method,path,**kwargs):self.path=path;order.append(path)
            def getresponse(self):
                data={'app':'SteadyRoute','worker_running':True} if self.path=='/api/control' else {'actual':'paused'}
                return mock.Mock(status=200,read=lambda:json.dumps(data).encode())
            def close(self):pass
        with mock.patch.object(service_cli.http.client,'HTTPConnection',Connection), mock.patch.object(service_cli.subprocess,'run',side_effect=lambda *a,**k:order.append('bootout')), mock.patch('builtins.print'):
            self.assertEqual(service_cli.main(['stop']),0)
        self.assertEqual(order,['/api/control','/api/control/change','bootout'])

    def test_stop_failure_does_not_unload_controller_or_claim_success(self):
        class Connection:
            def __init__(self,*a,**k):pass
            def request(self,method,path,**kwargs):self.path=path
            def getresponse(self):
                return mock.Mock(status=200 if self.path=='/api/control' else 409,
                    read=lambda:json.dumps({'app':'SteadyRoute'} if self.path=='/api/control' else {'error':'drain failed'}).encode())
            def close(self):pass
        with mock.patch.object(service_cli.http.client,'HTTPConnection',Connection), mock.patch.object(service_cli.subprocess,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'drain failed'):service_cli.main(['stop'])
            run.assert_not_called()

    def test_start_offline_reopens_controller_and_review_page_independently(self):
        fake_home=self.base/'home';plist=fake_home/'Library/LaunchAgents/com.steadyroute.plist'
        plist.parent.mkdir(parents=True);plist.write_text('synthetic launch agent')
        calls=[]
        class Connection:
            def __init__(self,*a,**k):pass
            def request(self,*a,**k):
                calls.append(True)
                if len(calls)==1:raise ConnectionRefusedError()
            def getresponse(self):return mock.Mock(status=200,read=lambda:b'{"app":"SteadyRoute"}')
            def close(self):pass
        with mock.patch.object(service_cli.http.client,'HTTPConnection',Connection), mock.patch.object(service_cli.pathlib.Path,'home',return_value=fake_home), mock.patch.object(service_cli.subprocess,'run',return_value=mock.Mock(returncode=0)) as run, mock.patch.object(service_cli.webbrowser,'open') as opened, mock.patch('builtins.print'):
            self.assertEqual(service_cli.main(['start']),0)
            self.assertEqual(run.call_args.args[0],['launchctl','bootstrap','gui/%d'%service_cli.os.getuid(),str(plist)])
            self.assertTrue(opened.call_args.args[0].endswith('/control'))


class InstanceAndPolicyPresentationTests(ServiceBase):
    def test_locked_group_has_fixed_policy_status_instead_of_switching(self):
        cfg=self.service.config()
        cfg['ai_line']=dict(enabled=True,country='TW',group_name='AI 台湾家宽线路',exit_policy='locked',locked_node=NODES[0])
        self.service.save_config(cfg)
        row=next(r for r in self.service.snapshot()['group_details'] if r['name']=='AI 台湾家宽线路')
        self.assertEqual(row['status'],'exit_locked')
        cfg['ai_line']['exit_policy']='adaptive';self.service.save_config(cfg)
        row=next(r for r in self.service.snapshot()['group_details'] if r['name']=='AI 台湾家宽线路')
        self.assertEqual(row['status'],'switching')

    def test_independent_entry_uses_its_own_instance_not_production_job(self):
        agent=self.base/'isolated-agent.plist';agent.write_text('synthetic')
        (self.base/'service-instance.json').write_text(json.dumps(dict(schema_version=1,label='com.steadyroute.acceptance',plist=str(agent),port=18788)))
        ports=[];routes=[]
        class Connection:
            def __init__(self,host,port,**kwargs):ports.append(port)
            def request(self,method,path,**kwargs):self.path=path;routes.append(path)
            def getresponse(self):return mock.Mock(status=200,read=lambda:json.dumps({'app':'SteadyRoute'} if self.path=='/api/control' else {'actual':'paused'}).encode())
            def close(self):pass
        with mock.patch.dict(service_cli.os.environ,{'STEADYROUTE_BASE_DIR':str(self.base),'STEADYROUTE_CONTROL_PORT':'17654'}),mock.patch.object(service_cli.http.client,'HTTPConnection',Connection),mock.patch.object(service_cli.subprocess,'run') as run,mock.patch('builtins.print'):
            service_cli.main(['stop'])
        self.assertEqual(ports,[18788,18788])
        self.assertEqual(run.call_args.args[0],['launchctl','bootout','gui/%d'%service_cli.os.getuid(),str(agent)])
        self.assertEqual(routes,['/api/control','/api/control/change'])


class OfflineInstanceStopTests(ServiceBase):
    def test_offline_stop_unloads_own_keepalive_job_before_reporting_success(self):
        agent=self.base/'isolated.plist';agent.write_text('synthetic')
        (self.base/'service-instance.json').write_text(json.dumps(dict(schema_version=1,label='com.steadyroute.acceptance',plist=str(agent),port=18788)))
        with mock.patch.dict(service_cli.os.environ,{'STEADYROUTE_BASE_DIR':str(self.base)}),mock.patch.object(service_cli.http.client,'HTTPConnection',side_effect=ConnectionRefusedError),mock.patch.object(service_cli.subprocess,'run') as run,mock.patch('builtins.print'):
            self.assertEqual(service_cli.main(['stop']),0)
        self.assertEqual(run.call_args.args[0],['launchctl','bootout','gui/%d'%service_cli.os.getuid(),str(agent)])
