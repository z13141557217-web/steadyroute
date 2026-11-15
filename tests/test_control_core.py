"""End-to-end lifecycle with the real isolated Mihomo and a real child router."""
import concurrent.futures
import fcntl
import http.client
import socket
import subprocess
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import quote
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import cycle_harness
import real_core
import settings_service
import service_control
from test_real_core import BASE_CONFIG, GEOSITE


@unittest.skipUnless(real_core.available(),'requires isolated Mihomo')
class ControlCoreTests(unittest.TestCase):
    def setUp(self):
        self.core=real_core.Core(GEOSITE)
        self.tmp=tempfile.TemporaryDirectory(prefix='sr-control-')
        self.base=pathlib.Path(self.tmp.name)
        config=dict(BASE_CONFIG)
        config['managed_lines']=[]
        self.config_path=self.base/'config.json'
        self.config_path.write_text(json.dumps(config))
        self.service=settings_service.SettingsService(self.config_path,self.base,self.core,
            clash_home=self.core.verge,core=real_core.CORE)
        self.group='AI 台湾家宽线路'
        self.target=self.core.proxies()[self.group]['now']
        self.original_groups,self.original_rules=self.core.groups(),self.core.rules()
        self.service.apply({'ai_line':dict(enabled=True,country='TW',group_name=self.group,
                                          exit_policy='locked',locked_node=self.target)})
        uid=self.service.target.resolve()[0]
        (self.core.verge/'profiles'/(uid+'.yaml')).write_text(json.dumps({'proxies':[
            dict(name=n,type='socks5',server='127.0.0.1',port=9) for n in real_core.NODES]}))
        self.control=service_control.Control(self.base,self.service)
        self.env=mock.patch.dict(os.environ,{
            'STEADYROUTE_CONTROLLER_SOCKET':self.core.socket,
            'STEADYROUTE_CLASH_HOME':str(self.core.verge),
            'STEADYROUTE_MIHOMO':real_core.CORE,
            'STEADYROUTE_LOG_DIR':str(self.base/'logs')})
        self.env.start()

    def tearDown(self):
        try:
            if self.control.worker is not None:
                try:self.control.pause()
                except Exception:
                    self.control.worker.kill();self.control.worker.wait(10)
        finally:
            self.env.stop();self.core.stop();self.tmp.cleanup()

    def start(self):
        try:
            self.control.change('start',self.control.preview('start')['fingerprint'])
        except Exception:
            print('Synthetic worker readiness:',getattr(self.control,'startup_evidence',None),file=sys.stderr)
            for p in (self.base/'logs').glob('*.log'):
                print('Synthetic worker log:',p.name,p.read_text(errors='replace')[-8000:],file=sys.stderr)
            raise

    def test_real_worker_stops_probes_and_late_writes_then_manual_restart(self):
        config=self.config_path.read_bytes()
        self.start()
        worker=self.control.worker
        self.assertEqual(self.control.snapshot()['actual'],'running')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.control.pause(),range(2)))
        self.assertTrue(all(s['actual']=='paused' for s in results))
        self.assertIsNotNone(worker.poll())
        self.assertFalse(self.control.snapshot()['worker_running'])
        state=self.base/'state.json'
        before=state.read_bytes() if state.exists() else None
        applied=self.service.applied_path.read_bytes()
        time.sleep(.3)
        self.assertEqual(state.read_bytes() if state.exists() else None,before)
        self.assertEqual(self.service.applied_path.read_bytes(),applied)
        self.assertEqual(self.config_path.read_bytes(),config)
        self.assertEqual(self.core.proxies()[self.group]['all'],[self.target])
        self.assertEqual(self.core.proxies()[self.group]['now'],self.target)
        self.start()
        self.assertNotEqual(self.control.worker.pid,worker.pid)
        self.control.pause()
        restarted=service_control.Control(self.base,self.service)
        self.assertFalse(restarted.snapshot()['desired_running'])

    def test_confirmed_handover_restores_current_profile_only_and_retains_target(self):
        before_config=self.config_path.read_bytes()
        self.control.change('release',self.control.preview('release')['fingerprint'],True)
        self.assertEqual(self.control.state['actual'],'released')
        self.assertEqual(self.core.groups(),self.original_groups)
        self.assertEqual(self.core.rules(),self.original_rules)
        self.assertEqual(self.config_path.read_bytes(),before_config)
        self.start()
        self.assertEqual(self.core.proxies()[self.group]['now'],self.target)
        self.assertEqual(self.core.proxies()[self.group]['all'],[self.target])

    def test_missing_target_in_new_subscription_blocks_without_any_write(self):
        source=self.core.verge/'profiles'/(self.service.target.resolve()[0]+'.yaml')
        source.write_text(json.dumps({'proxies':[dict(name='US ISP new',type='http',server='127.0.0.1',port=9)]}))
        before={p:p.read_bytes() for p in (self.config_path,self.service.applied_path,self.service.target.runtime)}
        with self.assertRaisesRegex(Exception,'缺少原锁定目标'):
            self.start()
        self.assertIsNone(self.control.worker)
        for p,data in before.items():self.assertEqual(p.read_bytes(),data)

    def test_target_preference_update_requires_confirmation_and_never_loads_core(self):
        alternatives=[x for x in self.control.target_candidates()['candidates'] if x['country']=='TW' and x['name']!=self.target]
        chosen=alternatives[0]
        plan=self.control.target_preview(chosen['country'],chosen['name'])
        before=self.service.target.runtime.read_bytes()
        with self.assertRaisesRegex(Exception,'确认'):
            self.control.save_target(chosen['country'],chosen['name'],plan['fingerprint'],False)
        self.control.save_target(chosen['country'],chosen['name'],plan['fingerprint'],True)
        self.assertEqual(self.service.target.runtime.read_bytes(),before)
        self.assertEqual(self.core.proxies()[self.group]['now'],self.target)
        self.assertEqual(self.service.config()['ai_line']['locked_node'],chosen['name'])
        with self.assertRaisesRegex(Exception,'偏离'):
            self.start()

    def test_explicit_off_succeeds_with_external_selector_drift_but_relock_is_guarded(self):
        # Saved target remains exact even when a user has externally replaced the group.
        native=self.core.original
        self.service.target.runtime.write_text(native)
        self.assertEqual(self.core.load(native)[0],204)
        backup=next(n for n in self.core.proxies()[self.group]['all'] if n!=self.target)
        self.core('PUT','/proxies/'+quote(self.group,safe=''),{'name':backup})
        with self.assertRaisesRegex(Exception,'当前选择'):
            self.service.preview({'ai_line':{'locked_node':real_core.NODES[2]}})
        self.service.apply({'ai_line':{'enabled':False}})
        self.assertFalse(self.service.config()['ai_line']['enabled'])


    def test_real_worker_crash_is_durable_failed_and_not_automatically_restarted(self):
        self.start();worker=self.control.worker
        worker.kill();worker.wait(5)
        snap=self.control.snapshot()
        self.assertEqual(snap['actual'],'failed')
        self.assertFalse(snap['desired_running'])
        time.sleep(.2)
        self.assertIsNone(self.control.worker)
        self.assertFalse(service_control.Control(self.base,self.service).state['desired_running'])
        self.assertEqual(self.core.proxies()[self.group]['now'],self.target)

    def test_control_plane_crash_stops_orphan_and_restart_cannot_retake_control(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        env=dict(os.environ,STEADYROUTE_BASE_DIR=str(self.base),STEADYROUTE_POLICY_CONFIG=str(self.config_path),STEADYROUTE_CONTROL_PORT=str(port))
        script=pathlib.Path(service_control.__file__).with_name('weighted_router.py')
        log=open(self.base/'parent.log','wb')
        def launch():return subprocess.Popen([sys.executable,str(script),'--daemon'],env=env,stdout=log,stderr=subprocess.STDOUT)
        def request(route,data=None):
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=25)
            headers={'Host':'127.0.0.1:%d'%port,'Origin':'http://127.0.0.1:%d'%port,'X-SteadyRoute':'1','Content-Type':'application/json'}
            try:
                c.request('GET' if data is None else 'POST',route,body=None if data is None else json.dumps(data),headers=headers)
                r=c.getresponse();body=json.loads(r.read());self.assertEqual(r.status,200,body);return body
            finally:c.close()
        def ready():
            for _ in range(100):
                try:return request('/api/control')
                except OSError:time.sleep(.05)
            self.fail('isolated control plane did not start')
        parent=launch()
        try:
            ready()
            plan=request('/api/control/preview',{'action':'start'})
            snap=request('/api/control/change',dict(action='start',fingerprint=plan['fingerprint']))
            self.assertTrue(snap['worker_running'])
            parent.kill();parent.wait(5)
            # Lock release proves that the orphan worker and its probe pool exited;
            # an absent HTTP page alone cannot prove this.
            with open(self.base/'router.lock','a') as worker_lock:
                for _ in range(300):
                    try:
                        fcntl.flock(worker_lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:time.sleep(.05)
                else:self.fail('orphan worker did not drain and exit')
            parent=launch();snap=ready()
            self.assertEqual(snap['actual'],'failed')
            self.assertFalse(snap['desired_running'])
            self.assertFalse(snap['worker_running'])
            self.assertEqual(self.core.proxies()[self.group]['now'],self.target)
        finally:
            if parent.poll() is None:
                parent.terminate()
                try:parent.wait(10)
                except subprocess.TimeoutExpired:
                    print("Synthetic control log:",(self.base/"parent.log").read_text(errors="replace")[-8000:],file=sys.stderr)
                    parent.kill();parent.wait(5)
            log.close()

    def test_source_changes_after_preview_are_refused_at_final_commit_boundary(self):
        source=self.core.verge/'profiles'/(self.service.target.resolve()[0]+'.yaml')
        original_manager=self.service.manager
        files={p:p.read_bytes() for p in self.service.target.resolve()[1:]+(self.service.target.runtime,)}
        fingerprint=self.control.preview('start')['fingerprint']
        def factory(*args,**kwargs):
            manager=original_manager(*args,**kwargs)
            original_apply=manager.apply
            def apply(*a,**kw):
                source.write_text(json.dumps({'proxies':[dict(name='US ISP changed',type='http',server='127.0.0.1',port=9)]}))
                return original_apply(*a,**kw)
            manager.apply=apply
            return manager
        with mock.patch.object(self.service,'manager',side_effect=factory):
            with self.assertRaisesRegex(Exception,'提交前订阅'):
                self.control.change('start',fingerprint)
        self.assertIsNone(self.control.worker)
        for p,data in files.items():self.assertEqual(p.read_bytes(),data)
        self.assertEqual(self.core.proxies()[self.group]['now'],self.target)

    def test_final_start_state_failure_drains_real_worker_and_restores_prestart_files(self):
        files={p:p.read_bytes() for p in self.service.target.resolve()[1:]+(self.service.target.runtime,self.service.applied_path)}
        original=self.control.save
        def save(**changes):
            if changes.get('actual')=='running':raise OSError('test final state failure')
            return original(**changes)
        with mock.patch.object(self.control,'save',side_effect=save):
            with self.assertRaisesRegex(Exception,'test final state failure'):self.start()
        self.assertIsNone(self.control.worker)
        self.assertFalse(self.control.state['desired_running'])
        self.assertEqual(self.control.state['actual'],'failed')
        for p,data in files.items():self.assertEqual(p.read_bytes(),data)
