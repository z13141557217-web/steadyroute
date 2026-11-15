"""Independent Finder/Terminal entry; never installs or unloads Mihomo."""
import argparse
import fcntl
import http.client
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import webbrowser
import settings_service


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start','stop','status'))
    args = parser.parse_args(argv)
    explicit_base = os.environ.get('STEADYROUTE_BASE_DIR')
    base = pathlib.Path(explicit_base) if explicit_base else pathlib.Path(__file__).resolve().parent
    if not explicit_base and not (base/'VERSION').is_file():
        raise RuntimeError('请使用已安装目录的独立入口；源码运行需明确指定隔离目录')
    context_path = base/'service-instance.json'
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    if context and (context.get('schema_version') != 1 or not isinstance(context.get('label'),str)
                    or not re.fullmatch(r'com\.steadyroute(?:\.[a-z0-9-]+)*',context['label'])
                    or not pathlib.Path(context.get('plist','')).is_absolute()
                    or not isinstance(context.get('port'),int) or isinstance(context.get('port'),bool)
                    or not 1 <= context['port'] <= 65535):
        raise RuntimeError('独立入口实例记录无效；未操作任何启动项')
    label = context.get('label','com.steadyroute')
    port = context['port'] if context else int(os.environ.get('STEADYROUTE_CONTROL_PORT') or 17654)
    url = 'http://127.0.0.1:%d' % port
    def call(route, data=None):
        c = http.client.HTTPConnection('127.0.0.1',port,timeout=210)
        headers={'Host':'127.0.0.1:%d'%port}
        if data is not None:
            headers.update({'Content-Type':'application/json','X-SteadyRoute':'1','Origin':url})
        try:
            c.request('POST' if data is not None else 'GET',route,
                      body=json.dumps(data) if data is not None else None,headers=headers)
            r=c.getresponse();result=json.loads(r.read())
            if r.status != 200:
                raise RuntimeError(result.get('error') or '控制页面未确认操作')
            if route=='/api/control' and result.get('app')!='SteadyRoute':
                raise RuntimeError('端口不是稳航控制页面，未操作')
            return result
        finally:
            c.close()
    plist=pathlib.Path(context['plist']) if context else pathlib.Path.home()/'Library/LaunchAgents/com.steadyroute.plist'
    domain='gui/%d'%os.getuid()
    def unload():
        try:
            subprocess.run(['launchctl','bootout',domain,str(plist)],check=True)
        except subprocess.CalledProcessError:
            probe = subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True,text=True)
            if probe.returncode != 113:
                raise RuntimeError('已保存停用，但启动项退出尚未核实；请重试停止入口')

    try:
        state=call('/api/control')
    except OSError:
        if args.action=='stop':
            base.mkdir(parents=True,exist_ok=True)
            # Do not claim an offline orphan is stopped just because HTTP is absent.
            with open(base/'control.lock','a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                path=base/'control-state.json'
                state=json.loads(path.read_text()) if path.exists() else dict(schema_version=1,login_start=True)
                if state.get('schema_version')!=1:
                    raise RuntimeError('控制记录版本未知；未覆盖')
                settings_service._atomic_json(path,dict(state,desired_running=False,actual='stopping'))
                try:
                    with open(base/'router.lock','a') as worker:
                        fcntl.flock(worker,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    settings_service._atomic_json(path,dict(state,desired_running=False,actual='failed',error='旧工作进程仍在退出；未核实停止'))
                    raise RuntimeError('旧工作进程仍在退出；保持停用，稍后重新运行停止入口')
                settings_service._atomic_json(path,dict(state,desired_running=False,actual='paused',error=None))
            unload()
            print('已保存保持停用，并确认控制页面和工作进程均未运行；本实例启动项已退出。原有固定规则未撤销。')
            return 0
        if args.action=='status':
            print('控制页面离线；不能仅凭页面离线判断规则已撤销。')
            return 1
        if not plist.exists():
            raise RuntimeError('没有找到稳航启动项，请先安装候选版本')
        registered=subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=False)
        if registered.returncode:
            subprocess.run(['launchctl','kickstart',domain+'/'+label],check=True)
        for _ in range(100):
            try:
                state=call('/api/control');break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('控制页面未启动；未报告成功')
    if args.action=='status':
        print(json.dumps(state,ensure_ascii=False,indent=2))
        return 0
    if args.action=='start':
        # The independent entry always opens the same reviewable preview; it cannot
        # silently adopt a different subscription or node while the page was closed.
        opened=webbrowser.open(url+'/control')
        print(('已打开独立运行控制页面。' if opened else '控制页面已可用，请打开 '+url+'/control。')+'请查看「预览并启动」并确认当前订阅和原目标。')
        return 0
    call('/api/control/change',{'action':'pause'})
    unload()
    print('已停止工作进程并退出控制页面；下次用启动入口打开。原有规则保留，未声称已交还 Clash。')
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('操作未完成：%s'%error,file=sys.stderr)
        sys.exit(1)
