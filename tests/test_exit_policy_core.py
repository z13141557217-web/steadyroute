"""Real independent clients and live backup/DIRECT counterexamples, never production.

Run with STEADYROUTE_MIHOMO. HTTP forwarding proxies stand in for transport leaves; the
Mihomo group/rule enforcement is real. These tests do not certify a supplier's public IP.
"""
import collections
import http.client
import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness
import ai_line
import clash_profile
import real_core

TARGET = '台湾 家宽 target [a].+'
BACKUP = '台湾 家宽 backup'
GROUP = 'AI isolated'


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Probe(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        self.send_response(200)
        self.end_headers()
        self.raw_requestline = self.rfile.readline(65537)
        if self.parse_request():
            self.do_GET()
        self.close_connection = True

    def do_GET(self):
        lab = self.server.lab
        path = urlsplit(self.path).path
        route = self.server.route
        lab.hits[(route + ('-via-'+self.headers['X-Lab-Route'] if self.headers.get('X-Lab-Route') else ''),path)] += 1
        if route == 'target' and (lab.error or (lab.probe_error and path == '/probe')):
            self.connection.close()
            return
        if route == 'target' and lab.delay:
            time.sleep(lab.delay)
        try:
            if route in ('target','backup'):
                upstream = http.client.HTTPConnection('127.0.0.1',lab.origin.server_port,timeout=4)
                upstream.request('GET',path,headers={'X-Lab-Route':route})
                response = upstream.getresponse()
                body = response.read()
                upstream.close()
            else:
                body = (self.headers.get('X-Lab-Route') or 'DIRECT').encode()
            self.send_response(200)
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body[:1]);self.wfile.flush()
            if path == '/hold' and route in ('target','backup'):
                lab.hold.wait(30)
            else:
                time.sleep(.05)  # keep a live /connections entry for proxy-chain evidence
            self.wfile.write(body[1:]);self.wfile.flush()
        except (OSError,http.client.HTTPException):
            self.close_connection=True


@unittest.skipUnless(real_core.available(), 'set STEADYROUTE_MIHOMO to a real Mihomo binary')
class KernelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory(prefix='sr-exit-',dir='/tmp')
        self.home=pathlib.Path(self.root.name)
        self.hits=collections.Counter();self.error=False;self.probe_error=False;self.delay=0
        self.hold=threading.Event()
        self.evidence=[]
        self.servers=[]
        for route in ('origin','target','backup'):
            server=Server(('127.0.0.1',0),Probe);server.lab=self;server.route=route
            self.servers.append(server)
            setattr(self,route,server)
            threading.Thread(target=server.serve_forever,daemon=True).start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.socket=str(self.home/'ctl.sock')
        self.log=open(self.home/'core.log','wb')
        self.process=None
        self.line=dict(enabled=True,group_name=GROUP,country='TW',exit_policy='locked',locked_node=TARGET)
        self.start(self.config())

    def tearDown(self):
        self.hold.set()
        if os.environ.get('STEADYROUTE_EXIT_EVIDENCE'):
            with open(os.environ['STEADYROUTE_EXIT_EVIDENCE'],'a',encoding='utf-8') as out:
                out.write(json.dumps(dict(test=self._testMethodName,events=self.evidence,
                    hits=[dict(route=key[0],path=key[1],count=value) for key,value in self.hits.items()]),ensure_ascii=False)+'\n')
        if self.process and self.process.poll() is None:
            self.process.terminate();self.process.wait(10)
        for server in self.servers:
            server.shutdown();server.server_close()
        self.log.close();self.root.cleanup()

    def config(self, nodes=None, mutate=None):
        names=[TARGET,BACKUP] if nodes is None else nodes
        proxies=[dict(name=n,type='http',server='127.0.0.1',port=self.target.server_port if n==TARGET else self.backup.server_port) for n in names]
        group=ai_line.group_definition(GROUP,'TW',True,self.line)
        if mutate=='backup':
            group['filter']='backup'
        if mutate=='direct':
            group['filter']='unmatchable';group['empty-fallback']='DIRECT'
        return json.dumps({'mixed-port':self.port,'mode':'rule','log-level':'debug',
            'hosts':{'ai.invalid':'127.0.0.1'},'proxies':proxies,
            'proxy-groups':[group,dict(name='Backup control',type='select',proxies=[BACKUP] if BACKUP in names else ['DIRECT'])],
            'rules':['DOMAIN,ai.invalid,'+GROUP,'MATCH,DIRECT']})

    def start(self,text):
        path=self.home/'config.yaml';path.write_text(text)
        clash_profile.core_validator(real_core.CORE,self.home)(text)
        self.process=subprocess.Popen([real_core.CORE,'-d',str(self.home),'-f',str(path),'-ext-ctl-unix',self.socket],stdout=self.log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                if self.ctl('GET','/version')[0]==200:return
            except OSError:pass
            time.sleep(.05)
        self.fail('isolated core did not start')

    def ctl(self,method,path,payload=None):
        return real_core.Core.__call__(self,method,path,payload)

    def get(self,path):
        status,body=self.ctl('GET',path);self.assertEqual(status,200,body);return json.loads(body)

    def load(self,text):
        clash_profile.core_validator(real_core.CORE,self.home)(text)
        status,body=self.ctl('PUT','/configs?force=true',dict(path='',payload=text))
        self.assertEqual(status,204,body)

    def request(self,path='/ok',host='ai.invalid',expected='target',chain=None):
        client=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        try:
            client.request('GET','http://%s:%d%s'%(host,self.origin.server_port,path),headers={'Connection':'close'})
            response=client.getresponse()
            self.evidence.append(dict(path=path,expected=expected,http_status=response.status))
            if expected is None:
                # Some HTTP clients receive a 502 instead of a socket close.
                self.assertGreaterEqual(response.status,400)
                return None
            self.assertEqual(response.status,200)
            if chain:
                chains=[entry['chains'] for entry in self.get('/connections')['connections']]
                self.evidence.append(dict(path=path,expected=expected,chains=chains))
                self.assertTrue(any(chain in item and GROUP in item for item in chains),chains)
            self.assertEqual(response.read().decode(),expected)
        except (OSError,http.client.HTTPException):
            self.evidence.append(dict(path=path,expected=expected,result='connection_error_or_timeout'))
            if expected is not None:raise
        finally:client.close()

    def assert_no_escape(self,action):
        # Controls stay reachable during every fault, with distinct tokens.
        self.request('/direct-control',host='127.0.0.1',expected='DIRECT')
        client=http.client.HTTPConnection('127.0.0.1',self.backup.server_port,timeout=2)
        client.request('GET','http://ai.invalid:%d/backup-control'%self.origin.server_port)
        self.assertEqual(client.getresponse().read(),b'backup');client.close()
        before=collections.Counter(self.hits)
        action()
        self.assertEqual(self.hits[('backup','/managed')],before[('backup','/managed')])
        self.assertEqual(self.hits[('origin','/managed')],before[('origin','/managed')])

    def test_client_chain_survives_slow_and_single_failed_probe_without_session_cleanup(self):
        self.delay=.15
        self.request('/slow',chain=TARGET)
        self.probe_error=True
        self.request('/probe',expected=None)
        self.request('/managed',chain=TARGET)
        # An existing healthy transfer stays open while router sees repeated probe failures.
        self.probe_error=False
        client=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        client.request('GET','http://ai.invalid:%d/hold'%self.origin.server_port)
        response=client.getresponse()
        self.probe_error=True
        self.request('/probe',expected=None)
        router=cycle_harness.load_router('real_exit_router')
        router.POLICY_CONFIG['ai_line']=self.line
        state=dict(groups={},nodes={TARGET:dict(effective_failure_streak=100,last_success=False)})
        before=self.get('/connections')['connections']
        held_ids={entry['id'] for entry in before if TARGET in entry['chains']}
        self.assertTrue(held_ids)
        router.evaluate_group(GROUP,[TARGET,BACKUP],self.get('/proxies')['proxies'],before,state,False)
        self.assertEqual(router.close_old_connections(GROUP,TARGET,before),0)
        self.assertTrue(held_ids.issubset({entry['id'] for entry in self.get('/connections')['connections']}))
        self.hold.set()
        self.assertEqual(response.read(),b'target');client.close()

    def test_connection_error_timeout_and_target_disconnection_never_use_live_escape_paths(self):
        self.error=True
        self.assert_no_escape(lambda:self.request('/managed',expected=None))
        self.error=False;self.delay=2.5
        self.assert_no_escape(lambda:self.request('/managed',expected=None))
        self.delay=0
        self.target.shutdown();self.target.server_close()
        self.assert_no_escape(lambda:self.request('/managed',expected=None))

    def test_remove_rename_empty_reload_controller_reconnect_and_process_resume(self):
        for names in ([BACKUP],['台湾 家宽 target renamed',BACKUP],[],[BACKUP]):
            with self.subTest(nodes=names):
                self.load(self.config(names))
                self.assertEqual(self.get('/proxies')['proxies'][GROUP]['all'],['REJECT'])
                self.assert_no_escape(lambda:self.request('/managed',expected=None))
        self.load(self.config())
        self.request('/restored',chain=TARGET)
        # Independent controller sessions reconnect on every call. SIGSTOP/CONT is an isolated process pause.
        import signal
        self.process.send_signal(signal.SIGSTOP);time.sleep(.05);self.process.send_signal(signal.SIGCONT)
        self.request('/resume',chain=TARGET)
        self.process.terminate();self.process.wait(10)
        self.start(self.config([BACKUP]))
        self.assert_no_escape(lambda:self.request('/managed',expected=None))

    def test_real_router_full_fast_and_resume_paths_with_independent_clients(self):
        from unittest import mock
        router=cycle_harness.load_router('real_full_fast_exit_router')
        router.PROFILE='auto_lock'
        router.POLICY_CONFIG.update(profile='auto_lock',ai_line=self.line,
            auto_lock={'include_groups':[GROUP]},mode='shadow')
        router.STATE_PATH=str(self.home/'router-state.json')
        router.CONFIRM_STAGGER_SECONDS=0
        local='http://127.0.0.1:%d/probe'%self.origin.server_port
        router.TEST_URL=router.FAST_PROBE_URL=local
        writes=[]
        def api(method,path,payload=None,timeout=None):
            if method in ('PUT','DELETE'):writes.append((method,path))
            status,body=self.ctl(method,path,payload)
            if status not in (200,204):
                raise router.ControllerHTTPError('HTTP %s: %s'%(status,body))
            return json.loads(body) if body else None
        state=router.state_contract.new_state()
        now=time.time()
        state['nodes']={node:cycle_harness.mature_node(60,now) for node in (TARGET,BACKUP)}
        router.save_state(state)
        try:
            with mock.patch.object(router,'api_request',side_effect=api), \
                    mock.patch.object(ai_line,'business_urls',return_value=[local]), \
                    mock.patch.object(router.logging_setup,'write_event'):
                router.run_cycle(False)
                self.request('/managed',chain=TARGET)
                held=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
                held.request('GET','http://ai.invalid:%d/hold'%self.origin.server_port)
                held_response=held.getresponse()
                held_ids={entry['id'] for entry in self.get('/connections')['connections'] if TARGET in entry['chains']}
                self.assertTrue(held_ids)
                self.probe_error=True
                router.run_fast_tick(False)
                router.run_cycle(False)
                self.request('/managed',chain=TARGET)
                self.assertTrue(held_ids.issubset({entry['id'] for entry in self.get('/connections')['connections']}))
                self.hold.set();self.assertEqual(held_response.read(),b'target');held.close()
                # Controller transport disappears while the data plane stays reachable.
                offline_socket=str(self.home/'offline.sock')
                os.rename(self.socket,offline_socket)
                try:
                    with self.assertRaises(OSError):router.run_cycle(False)
                    self.request('/managed')
                    self.evidence.append(dict(controller='offline',data_plane='target'))
                finally:os.rename(offline_socket,self.socket)
                router.run_cycle(False)
                # Simulate macOS wall-clock/monotonic divergence, then run recovery cycle.
                router.RUNTIME['last_tick_wall']=time.time()-3600
                router.RUNTIME['last_tick_mono']=time.monotonic()
                self.assertEqual(router.run_fast_tick(False),'resume')
                router.run_cycle(False)
                self.request('/managed',chain=TARGET)
                self.load(self.config([BACKUP]))
                router.run_cycle(False)
                router.run_fast_tick(False)
                self.assert_no_escape(lambda:self.request('/managed',expected=None))
                self.load(self.config([]))
                router.run_cycle(False)
                self.assert_no_escape(lambda:self.request('/managed',expected=None))
            self.assertEqual(writes,[], 'no router selection PUT or connection DELETE')
            self.evidence.append(dict(router_full_fast_resume_writes=writes))
        finally:
            for pool in (router.PROBE_POOL,router.CONTROL_POOL):
                if pool:pool.shutdown(wait=True)

    def test_inherited_healthy_session_is_reported_without_forced_cleanup(self):
        import exit_policy
        self.load(self.config(mutate='backup'))
        held=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        held.request('GET','http://ai.invalid:%d/hold'%self.origin.server_port)
        response=held.getresponse()
        self.load(self.config())
        self.request('/new-locked',chain=TARGET)
        evidence=exit_policy.status(self.line,self.get('/proxies')['proxies'],True,
                                    connections=self.get('/connections')['connections'])
        self.assertEqual(evidence['policy_status'],'inherited_sessions')
        self.assertIsNone(evidence['effective_mode'])
        self.assertGreaterEqual(evidence['inherited_connections'],1)
        self.hold.set();self.assertEqual(response.read(),b'backup');held.close()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            evidence=exit_policy.status(self.line,self.get('/proxies')['proxies'],True,
                                        connections=self.get('/connections')['connections'])
            if evidence['effective_mode']=='locked':break
            time.sleep(.01)
        self.assertEqual(evidence['effective_mode'],'locked')
        self.evidence.append(dict(inherited_session='natural_completion',final_status=evidence['policy_status']))

    def test_mutations_are_detected_by_real_backup_and_direct_hits(self):
        self.load(self.config(mutate='backup'))
        self.request('/managed',expected='backup',chain=BACKUP)
        self.assertGreater(self.hits[('backup','/managed')],0)
        self.load(self.config(mutate='direct'))
        self.request('/direct-mutant',expected='DIRECT',chain='DIRECT')
        self.assertGreater(self.hits[('origin','/direct-mutant')],0)
