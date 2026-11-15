"""Local control plane. Only the child router probes/routes/maintains subscriptions.

The control plane survives pause and serves the UI. All changes are serialised;
desired stop is durable BEFORE draining the worker. No implicit worker restart.
"""
import argparse
import fcntl
import hashlib
import http.client
import http.server
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit, parse_qs
import ai_line
import clash_profile
import exit_policy
import settings_service


class Control:
    def __init__(self, base, service, spawn=subprocess.Popen, worker_script=None):
        self.base = pathlib.Path(base)
        self.service = service
        self.path = self.base / 'control-state.json'
        self.lock = threading.RLock()
        self.spawn = spawn
        self.worker_script = worker_script or pathlib.Path(__file__).with_name('weighted_router.py')
        self.worker = None
        self.worker_port = None
        self.worker_key = uuid.uuid4().hex
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if (self.state.get('schema_version') != 1
                    or not all(isinstance(self.state.get(k),bool) for k in ('desired_running','login_start'))
                    or self.state.get('actual') not in ('paused','running','starting','stopping','released','releasing','failed')):
                raise RuntimeError('运行控制记录版本不受支持；保持停止，未覆盖原记录')
        else:
            self.state = dict(schema_version=1, desired_running=False, login_start=True,
                              actual='paused', error=None)

    def save(self, **changes):
        proposed = dict(self.state, **changes, updated_at=int(time.time()))
        settings_service._atomic_json(self.path, proposed)
        self.state = proposed

    def snapshot(self):
        with self.lock:
            if self.worker is not None and self.worker.poll() is not None:
                self.worker = None
                self.save(actual='failed', desired_running=False,
                          error='工作进程意外退出；保持停止，请核对后手动启动')
            result = dict(self.state)
            result['subscription_scope'] = self.service.scope.snapshot()
            result.update(app="SteadyRoute", controller_running=True, worker_running=self.worker is not None,
                          worker_pid=self.worker.pid if self.worker else None,
                          protection='未重新核实；暂停保留已有规则，进程停止不代表规则撤销')
            if self.state['actual'] == 'released':
                result['protection'] = '上次交还操作已核对撤销当时订阅的稳航内容和固定出口保护；其他订阅的恢复记录保留。当前规则仍可能被 Clash 或其他程序修改。'
            return result

    def _inputs(self):
        uid, groups, rules = self.service.target.resolve()
        source = self.service.target.profile_dir / (uid + '.yaml')
        paths = [self.service.config_path, self.service.applied_path, self.service.target.profiles_yaml,
                 groups, rules, self.service.target.runtime, source, self.service.scope.path]
        digest = hashlib.sha256()
        for path in paths:
            digest.update(str(path).encode())
            digest.update(path.read_bytes() if path.exists() else b'<absent>')
        return uid, source, digest

    @staticmethod
    def source_names(source):
        if not source.exists() or source.is_symlink():
            raise clash_profile.ProfileError('当前订阅源文件无法核对；保持停用，请先在 Clash 核对订阅')
        text = source.read_text(encoding='utf-8')
        try:
            data = json.loads(text)
        except ValueError:
            lines = text.splitlines()
            at = clash_profile._find_key(lines, 'proxies')
            if at is None or lines[at].partition(':')[2].strip():
                raise clash_profile.ProfileError('当前订阅文件格式无法安全核对原目标；保持停用，请核对目标')
            indent, end = clash_profile._sequence(lines, at)
            names = [clash_profile._item_name(lines[first]) for first, _ in
                     clash_profile._split_items(lines, at+1, end, indent or 0)]
            if any(name is None for name in names):
                raise clash_profile.ProfileError('当前订阅文件节点名无法完整核对；保持停用')
            return names
        return [item.get('name') for item in data.get('proxies') or []]

    def preview(self, action):
        self.service.scope.require()
        if action not in ('start', 'release'):
            raise ValueError('未知运行操作')
        uid, source, digest = self._inputs()
        config, applied = self.service.config(), self.service.applied()
        line = config.get('ai_line') or {}
        manager = self.service.manager(config, applied)
        owned = manager.profile_record(uid)
        result = dict(action=action, profile_uid=uid, country=line.get('country'),
                      saved_ai_enabled=bool(line.get('enabled')), locked_node=line.get('locked_node'),
                      policy=exit_policy.mode(line), remove_groups=owned.get('groups') or [],
                      remove_rules=owned.get('rules') or [], requires_confirmation=action=='release')
        if action == 'start':
            self.service.check_core()
            data = self.service.proxies()
            self.service.writer().check_current(self.service.target.runtime.read_text())
            if line.get('enabled') and exit_policy.mode(line) == 'locked' and not line.get('binding_missing'):
                target = line.get('locked_node')
                if target not in self.source_names(source) or not ai_line.is_node(data.get(target)):
                    raise clash_profile.ProfileError('当前订阅缺少原锁定目标；保持停用。请补回原目标，或在 Clash 核对后明确更新目标；不会自动选其他节点或 DIRECT')
                if target not in ai_line.members(line['country'], [target]):
                    raise clash_profile.ProfileError('原目标不符合保存地区的家宽节点要求；保持停用')
                current = (data.get(line.get('group_name')) or {}).get('now')
                if current not in (None, 'REJECT', target):
                    raise clash_profile.ProfileError('当前选择已偏离保存的锁定目标；保持停用，请核对或明确更新目标')
                digest.update(json.dumps([target, current], ensure_ascii=False).encode())
            if self.service._wants_lines(config):
                plan = manager.validated_plan()
                result['lines'] = plan['lines']
        else:
            marked = any(clash_profile.MARK_BEGIN in p.read_text() for p in self.service.target.resolve()[1:])
            if marked and not owned.get('groups'):
                raise clash_profile.ProfileError('当前订阅有受管标记但没有可信归属记录；保持停止，请先按备份核对，未撤销')
            if owned.get('groups'):
                plan = manager.disable_plan()
                self.service.writer().validate(plan['texts']['runtime'])
        result['fingerprint'] = digest.hexdigest()
        return result

    def target_candidates(self):
        self.service.scope.require()
        with self.lock:
            if self.snapshot()['worker_running']:
                raise RuntimeError('请先暂停，再核对下次启动目标')
            _, source, _ = self._inputs()
            names = self.source_names(source)
            data = self.service.proxies()
            config = self.service.config()
            line = config.get('ai_line') or {}
            import regions
            candidates = [dict(name=name,country=regions.region_of(name)[0]) for name in names
                          if ai_line.is_node(data.get(name)) and regions.is_residential(name)
                          and not regions.is_notice(name) and regions.region_of(name)[0] not in ai_line.AI_UNSUPPORTED
                          and regions.region_of(name)[0] != regions.OTHER_REGION[0]]
            return dict(candidates=candidates,saved_target=line.get('locked_node'),
                        saved_country=line.get('country'))

    def target_preview(self, country, node):
        choices = self.target_candidates()['candidates']
        if dict(name=node,country=country) not in choices:
            raise clash_profile.ProfileError('请选择当前订阅中可核对的明确地区家宽节点；未保存')
        uid, _, digest = self._inputs()
        digest.update(json.dumps([country,node],ensure_ascii=False).encode())
        line = self.service.config().get('ai_line') or {}
        return dict(fingerprint=digest.hexdigest(),profile_uid=uid,country=country,locked_node=node,
                    previous_country=line.get('country'),previous_target=line.get('locked_node'))

    def save_target(self, country, node, fingerprint, confirmed):
        with self.lock:
            if confirmed is not True:
                raise RuntimeError('更改下次启动的固定目标需要明确确认')
            plan = self.target_preview(country,node)
            if plan['fingerprint'] != fingerprint:
                raise RuntimeError('预览后当前订阅或设置已变化；请重新核对')
            config = self.service.config()
            line = dict(config.get('ai_line') or {})
            line.pop('slow_exit',None)
            line.pop('binding_missing',None)
            line.update(enabled=True,exit_policy='locked',country=country,locked_node=node)
            line.setdefault('group_name',ai_line.DEFAULT_AI_GROUP)
            config['ai_line'] = line
            if (config.get('netcoffee') or {}).get('enabled'):
                config['netcoffee'].setdefault('bindings', {})[plan['profile_uid']] = {
                    key: line[key] for key in ('country','locked_node','group_name')}
                config['probe_nodes'] = [node]
            old_text = self.service.config_path.read_text()
            self.service.save_config(config)
            try:
                self.save(actual='paused',desired_running=False,error=None)
            except Exception as error:
                clash_profile._atomic_write(self.service.config_path,old_text)
                raise RuntimeError('运行记录保存失败；原目标偏好已恢复：%s' % error)
            return dict(saved=True,worker_started=False,clash_changed=False)

    def set_login(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('登录时启动必须为是或否')
        with self.lock:
            self.save(login_start=enabled)
            return self.snapshot()

    def _worker_call(self, method, route, data=None, timeout=10):
        c = http.client.HTTPConnection('127.0.0.1', self.worker_port, timeout=timeout)
        headers = {'Host':'127.0.0.1:%d' % self.worker_port, 'X-SteadyRoute-Worker':self.worker_key}
        body = None
        if method == 'POST':
            headers.update({'Origin':'http://127.0.0.1:%d' % self.worker_port,
                            'X-SteadyRoute':'1', 'Content-Type':'application/json'})
            body = json.dumps(data or {})
        try:
            c.request(method, route, body=body, headers=headers)
            response = c.getresponse()
            return response.status, response.getheader('Content-Type'), response.read()
        finally:
            c.close()

    def _drain_worker(self):
        if self.worker is None:
            return
        if self.worker.poll() is not None:
            self.worker.wait()
            self.worker = None
            return
        code, _, body = self._worker_call('POST', '/api/control/quiesce', timeout=195)
        if code != 200 or not json.loads(body).get('drained'):
            raise RuntimeError('工作进程未完成停止屏障，尚未报告停止成功')
        self.worker.terminate()
        self.worker.wait(timeout=15)
        self.worker = None

    def pause(self):
        with self.lock:
            self.snapshot()
            if self.worker is None and self.state['actual'] in ('paused','released'):
                self.save(desired_running=False)
                return self.snapshot()
            self.save(desired_running=False, actual='stopping', error=None)
            try:
                self._drain_worker()
                self.save(actual='paused', error=None)
            except Exception as error:
                self.save(actual='failed', error='停止尚未核实：%s' % error)
                raise
            return self.snapshot()

    def _guard_preview(self, action, expected):
        self.service.scope.require()
        _, _, digest = self._inputs()
        if action == 'start':
            line = self.service.config().get('ai_line') or {}
            if line.get('enabled') and exit_policy.mode(line) == 'locked':
                data = self.service.proxies()
                target = line.get('locked_node')
                if not ai_line.is_node(data.get(target)):
                    raise clash_profile.ProfileError('提交前原目标已消失；保持停用')
                digest.update(json.dumps([target,(data.get(line.get('group_name')) or {}).get('now')],ensure_ascii=False).encode())
        if digest.hexdigest() != expected:
            raise clash_profile.ProfileError('提交前订阅、设置或目标已变化；保持停用，请重新预览')

    def _rollback_manager(self, manager, original):
        folder = pathlib.Path(manager.last_backup)
        texts = {key:(folder/(key+'.yaml')).read_text() for key in ('groups','rules','runtime')}
        previous_meta = json.loads((folder/'meta.json').read_text())
        def unchanged():
            uid, groups, rules = self.service.target.resolve()
            for key,path in (('groups',groups),('rules',rules),('runtime',self.service.target.runtime)):
                if path.read_text() != manager.last_written[key]:
                    raise clash_profile.ProfileError('回滚前文件已被外部修改；未覆盖，请核对原事务备份')
        unchanged()
        self.service.writer().commit(texts['groups'], texts['rules'], texts['runtime'],
            {'action':'control-persistence-rollback','profile_uid':previous_meta.get('profile_uid'),
             'binding':previous_meta.get('binding')},precondition=unchanged)
        settings_service._atomic_json(self.service.applied_path,original)

    def _record(self, manager, original, new):
        try:
            settings_service._atomic_json(self.service.applied_path,new)
        except Exception as error:
            self._rollback_manager(manager,original)
            raise clash_profile.ProfileError('记录保存失败；已恢复内核、文件与原记录：%s' % error)

    def change(self, action, fingerprint=None, confirmed=False):
        with self.lock:
            self.snapshot()
            if action == 'pause':
                return self.pause()
            if action == 'start' and self.worker is not None and self.state['actual']=='running':
                return self.snapshot()
            if action == 'release' and confirmed is not True:
                raise clash_profile.ProfileError('交还 Clash 会撤销固定出口保护，必须明确确认')
            plan = self.preview(action)
            if fingerprint != plan['fingerprint']:
                raise clash_profile.ProfileError('预览后订阅、配置或目标已变化；请重新预览')
            if action == 'release':
                self.pause()
                self.save(actual='releasing', desired_running=False, error=None)
                manager = None
                recorded = False
                try:
                    # The worker may have finished a transaction while draining; re-preview.
                    fresh = self.preview('release')
                    if fresh['fingerprint'] != fingerprint:
                        raise clash_profile.ProfileError('停止期间配置已变化；现已暂停，请重新预览撤销范围')
                    original = self.service.applied()
                    if fresh['remove_groups']:
                        manager = self.service.manager(self.service.config(), original)
                        new = manager.disable(precondition=lambda:self._guard_preview('release',fingerprint))
                        self._record(manager, original, new)
                        recorded = True
                    self.save(actual='released', released_profile=plan['profile_uid'], error=None)
                except Exception as error:
                    if manager is not None and recorded:
                        try:
                            self._rollback_manager(manager,original)
                        except Exception as rollback_error:
                            error = RuntimeError('%s；恢复未完成：%s' % (error,rollback_error))
                    self.save(actual='failed', error='未完成交还 Clash：%s' % error)
                    raise
            elif action == 'start':
                self.save(actual='starting', desired_running=False, error=None)
                manager = None
                recorded = False
                try:
                    with open(self.base/'router.lock','a') as worker_lock:
                        try:
                            fcntl.flock(worker_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError:
                            raise RuntimeError('旧工作进程仍在退出；保持停用，请稍后重试')
                    config, original = self.service.config(), self.service.applied()
                    if self.service._wants_lines(config):
                        manager = self.service.manager(config, original)
                        _, new = manager.apply(resume=True,precondition=lambda:self._guard_preview('start',fingerprint))
                        manager.last_backup = new['backup']
                        self._record(manager, original, new)
                        recorded = True
                    self.preview('start')
                    with socket.socket() as probe:
                        probe.bind(('127.0.0.1', 0))
                        self.worker_port = probe.getsockname()[1]
                    env = dict(os.environ, STEADYROUTE_WORKER='1', STEADYROUTE_WORKER_KEY=self.worker_key, STEADYROUTE_PARENT=str(os.getpid()),
                               STEADYROUTE_WORKER_PORT=str(self.worker_port), STEADYROUTE_BASE_DIR=str(self.base),
                               STEADYROUTE_POLICY_CONFIG=str(self.service.config_path))
                    self.worker = self.spawn([sys.executable, str(self.worker_script), '--daemon'], env=env)
                    self.startup_evidence = {"stage":"waiting_for_worker_http","expected_pid":self.worker.pid}
                    deadline = time.monotonic()+20
                    while True:
                        if self.worker.poll() is not None:
                            raise RuntimeError('工作进程未能启动；已有规则可能保留')
                        try:
                            code, _, ready = self._worker_call('GET','/api/control/worker', timeout=1)
                            observed_pid = json.loads(ready).get('pid') if code == 200 else None
                            self.startup_evidence = {"stage":"worker_http_response","http_status":code,"expected_pid":self.worker.pid,"observed_pid":observed_pid}
                            if code == 200 and observed_pid == self.worker.pid:
                                break
                        except OSError as connection_error:
                            self.startup_evidence = {"stage":"waiting_for_worker_http","error_type":type(connection_error).__name__,"expected_pid":self.worker.pid}
                        if time.monotonic() > deadline:
                            # No successful admission proof: drain rather than kill a live transaction.
                            self.pause()
                            raise TimeoutError('启动状态尚未核实，已暂停')
                        time.sleep(.1)
                    self.save(actual='running', desired_running=True, error=None)
                except Exception as error:
                    try:
                        self._drain_worker()
                        if manager is not None and recorded:
                            self._rollback_manager(manager,original)
                    except Exception as recovery_error:
                        error = RuntimeError('%s；停止或恢复尚未完成：%s' % (error,recovery_error))
                    self.save(actual='failed', desired_running=False, error='启动未完成：%s' % error)
                    raise
            return self.snapshot()


def make_handler(control, router, synthetic=False):
    class Handler(router.DashboardHandler):
        def do_GET(self):
            if not router.host_allowed(self.headers.get('Host'), self.server.server_address[1]):
                self._reply(421,'text/plain',b'misdirected request',api=True)
                return
            path = urlsplit(self.path).path
            if path == '/api/control':
                self.reply_json(200, control.snapshot())
                return
            if path == '/api/control/targets':
                try:
                    self.reply_json(200,control.target_candidates())
                except Exception as error:
                    self.reply_json(409,{'error':str(error)})
                return
            if path == '/assets/control.js':
                self._reply(200,'text/javascript; charset=utf-8',pathlib.Path(__file__).with_name('control.js').read_bytes())
                return
            if path in ('/', '/settings', '/control'):
                if path == '/control' or not control.snapshot()['worker_running']:
                    page = pathlib.Path(__file__).with_name('control.html').read_bytes()
                else:
                    code, content, page = control._worker_call('GET', path)
                    if code != 200:
                        self._reply(code, content, page)
                        return
                if synthetic:
                    theme = (parse_qs(urlsplit(self.path).query).get('theme') or [''])[0]
                    if theme in ('light','dark'):
                        page = page.replace(b'<html',('<html data-theme="'+theme+'"').encode(),1)
                    page = page.replace(b'<body>', '<body><p style="padding:12px 24px;color:var(--ink);background:var(--accent-soft)">隔离合成预览 · 仅含测试节点；不读取生产订阅，不代表真实线路健康。</p>'.encode())
                page = page.replace(b'</body>', b'<script src="/assets/control.js"></script></body>')
                self._reply(200,'text/html; charset=utf-8',page)
                return
            if path.startswith('/api/'):
                if control.snapshot()['worker_running']:
                    try:
                        code, content, body = control._worker_call('GET',self.path)
                        self._reply(code,content,body,api=True)
                    except Exception:
                        self.reply_json(503, {'error':'工作进程不可用；运行状态请看总开关'})
                elif path in ('/api/status','/api/v1/status'):
                    self.reply_json(200, {'service':dict(version=router.read_service_version(),
                        controller_connected=False, profile='auto_lock', lifecycle=control.snapshot()), 'groups':[], 'stale':True})
                else:
                    self.reply_json(409, {'error':'稳航已暂停；没有当前检测数据或工作进程。请先预览并启动。'})
                return
            super().do_GET()

        def reply_json(self, code, payload):
            self._reply(code,'application/json; charset=utf-8',json.dumps(payload,ensure_ascii=False).encode(),api=True)

        def do_POST(self):
            port = self.server.server_address[1]
            if not router.settings_request_allowed(self.headers,port):
                self.reply_json(403,{'error':'forbidden'})
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 <= length <= router.SETTINGS_BODY_LIMIT:
                    raise ValueError('请求过大')
                raw = self.rfile.read(length)
                body = json.loads(raw or b'{}')
                if not isinstance(body,dict):
                    raise ValueError('请求必须为对象')
                path = urlsplit(self.path).path
                if path == '/api/control/target-preview':
                    with control.lock:
                        result = control.target_preview(body.get('country'),body.get('locked_node'))
                elif path == '/api/control/target-save':
                    result = control.save_target(body.get('country'),body.get('locked_node'),body.get('fingerprint'),body.get('confirmed'))
                elif path == '/api/control/preview':
                    with control.lock:
                        result = control.preview(body.get('action'))
                elif path == '/api/control/change':
                    result = control.change(body.get('action'),body.get('fingerprint'),body.get('confirmed'))
                elif path == '/api/control/login':
                    result = control.set_login(body.get('enabled'))
                elif path in ('/api/settings/preview','/api/settings/apply','/api/settings/sync'):
                    with control.lock:
                        if not control.snapshot()['worker_running'] or control.state['actual'] != 'running':
                            raise RuntimeError('稳航工作进程已停止；不会保存或重新接管，请先预览启动')
                        code, content, data = control._worker_call('POST',path,body,timeout=180)
                        self._reply(code,content,data,api=True)
                        return
                else:
                    self.reply_json(404,{'error':'not_found'})
                    return
                self.reply_json(200,result)
            except Exception as error:
                self.reply_json(409,{'error':str(error)})
    return Handler


def main():
    import weighted_router as router
    base = pathlib.Path(router.BASE_DIR)
    base.mkdir(parents=True, exist_ok=True)
    lock = open(base/'control.lock','w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('稳航控制页面已在运行')
    service = settings_service.SettingsService(router.POLICY_CONFIG_PATH,base,router.controller_call,
        clash_home=os.environ.get('STEADYROUTE_CLASH_HOME'), core=os.environ.get('STEADYROUTE_MIHOMO'))
    control = Control(base, service)
    # A restart never inherits an unverified running PID. Orphan workers close their gate
    # when their parent changes and exit; the router lock also prevents overlap.
    resume = (control.state['desired_running'] and control.state['login_start']
              and control.state['actual'] == 'paused')
    interrupted = control.state['actual'] not in ('paused','released')
    control.save(actual='failed' if interrupted else control.state['actual'],desired_running=False,
                 error='上次运行或操作未正常完成；保持停用，请重新核对' if interrupted else None)
    server = router.QuietHTTPServer(('127.0.0.1',int(os.environ.get('STEADYROUTE_CONTROL_PORT','17654'))),
                                    make_handler(control,router))
    if resume:
        def boot():
            try:
                control.change('start',control.preview('start')['fingerprint'])
            except Exception as error:
                control.save(actual='failed',desired_running=False,error=str(error))
        threading.Thread(target=boot,daemon=True).start()
    def shutdown(_signal,_frame):
        def finish():
            try:
                wanted = control.state['desired_running']
                control.pause()
                # System logout can resume only with both saved settings; a manual pause
                # already saved desired_running=False before stopping the controller.
                control.save(desired_running=wanted)
            finally:
                server.shutdown()
        threading.Thread(target=finish,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown)
    signal.signal(signal.SIGINT,shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
