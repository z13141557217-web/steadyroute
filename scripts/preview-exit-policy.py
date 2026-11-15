#!/usr/bin/env python3
"""Interactive isolated settings/dashboard preview. Never reads installed production state."""
import argparse
import http.server
import json
import pathlib
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
import cycle_harness
import real_core
import settings_service
import ai_line
import candidate_registry
import exit_policy
from test_real_core import GEOSITE, BASE_CONFIG


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18772)
    cases = ('empty', 'consistent', 'same-country', 'other-country', 'direct', 'unknown', 'mixed', 'offline', 'fetch-error', 'stale', 'api-error')
    parser.add_argument('--case', choices=cases, default='empty')
    args=parser.parse_args()
    if not real_core.available():
        parser.error('set STEADYROUTE_MIHOMO to an independent core executable')
    with tempfile.TemporaryDirectory(prefix='sr-policy-preview-') as directory:
        core=real_core.Core(GEOSITE)
        try:
            root=pathlib.Path(directory);config=root/'config.json'
            config.write_text(json.dumps(BASE_CONFIG))
            service=settings_service.SettingsService(config,root,core,clash_home=core.verge,core=real_core.CORE)
            group='AI 台湾家宽线路';target=core.proxies()[group]['now']
            service.apply({'ai_line':dict(enabled=True,country='TW',group_name=group,exit_policy='locked',locked_node=target)})
            selected = {'case': args.case}
            data = core.proxies()
            same = next(n for n in data if n != target and n.startswith('台湾') and ai_line.is_node(data[n]))
            other = next(n for n in data if '日本' in n and ai_line.is_node(data[n]))
            def sessions():
                def item(chain): return dict(metadata={'host':'claude.ai'}, chains=chain)
                samples = {'empty':[], 'consistent':[item([target,group])],
                           'same-country':[item([same,'Legacy AI'])], 'other-country':[item([other,'Legacy AI'])],
                           'direct':[item(['DIRECT'])], 'unknown':[item([])]}
                samples['mixed'] = samples['consistent'] + samples['direct'] + samples['unknown'] + [dict(metadata={'host':'ordinary.invalid'},chains=['DIRECT'])]
                return samples.get(selected['case'], samples['consistent'])
            def controller(method, path, payload):
                if selected['case'] == 'offline' and method == 'GET': raise OSError('isolated offline fixture')
                if method == 'GET' and path == '/connections':
                    if selected['case'] == 'fetch-error': raise OSError('isolated connection-read fixture')
                    return 200, json.dumps({'connections':sessions()})
                return core(method,path,payload)
            service.controller = controller
            def settings_snapshot():
                result = service.snapshot()
                if selected['case'] == 'stale':
                    now = time.time()
                    result['exit_policy'] = exit_policy.status(service.config()['ai_line'], core.proxies(), True,
                        connections=sessions(), managed_rules=service.applied()['rules'], now=now,
                        checked_at=now-60, connections_checked_at=now-60)
                return result
            router=cycle_harness.load_router('isolated_policy_preview')
            router.PROFILE='auto_lock';router.SETTINGS=service

            def status():
                now=time.time();data=core.proxies();state=router.state_contract.new_state()
                state['updated_at']=int(now)
                state['last_probe_at']=int(now)
                state['controller_connected']=selected['case'] != 'offline'
                state['proxies_observed_at']=now - (60 if selected['case']=='stale' else 0)
                state['connections_observed_at']=state['proxies_observed_at'] if selected['case'] != 'fetch-error' else None
                router.POLICY_CONFIG.update(service.config())
                router.refresh_auto_lock(state,data,now)
                for node,proxy in data.items():
                    if ai_line.is_node(proxy):state['nodes'][node]=cycle_harness.mature_node(120,now)
                candidate_registry.reconcile(router.POLICY_CONFIG,state,data,now)
                return router.build_status_snapshots(state,data,sessions(),now=now)['legacy']

            class Handler(http.server.BaseHTTPRequestHandler):
                def reply(self,payload,mime='application/json',code=200):
                    body=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False).encode()
                    self.send_response(code);self.send_header('Content-Type',mime+'; charset=utf-8')
                    self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
                    self.end_headers();self.wfile.write(body)

                def do_GET(self):
                    path=urlsplit(self.path).path
                    if selected['case']=='api-error' and path in ('/api/settings','/api/status'):
                        return self.reply({'error':'isolated transport fixture'},code=503)
                    if path=='/api/settings':return self.reply(settings_snapshot())
                    if path=='/api/ai-check':return self.reply(service.check())
                    if path=='/api/status':return self.reply(status())
                    names={'/':'dashboard.html','/settings':'settings.html','/guide':'guide.html','/changelog':'changelog.html','/assets/pages.css':'pages.css'}
                    if path not in names:return self.reply({'error':'preview only'},code=404)
                    body=(ROOT/'src/steadyroute'/names[path]).read_bytes()
                    if path!='/assets/pages.css':
                        options=''.join('<option%s>%s</option>'%(' selected' if c==selected['case'] else '',c) for c in cases)
                        banner='<body><p style="padding:12px 24px;background:#fff2cd;color:#684b00;margin:0;font-size:14px">隔离开发预览 · 合成节点与连接元数据 · 不连接生产 · 场景 <select aria-label="验收场景" onchange="fetch(\'/api/preview/case\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({case:this.value})}).then(()=>{if(this.value!==\'api-error\')location.reload()})">'+options+'</select></p>'
                        body=body.replace(b'<body>',banner.encode(),1)
                    self.reply(body,'text/css' if path.endswith('.css') else 'text/html')

                def do_POST(self):
                    try:
                        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                        if self.path=='/api/preview/case':
                            if body.get('case') not in cases: return self.reply({'error':'unknown case'},code=400)
                            selected['case']=body['case']
                            return self.reply({'case':selected['case']})
                        if self.path=='/api/settings/preview':return self.reply(service.preview(body))
                        if self.path=='/api/settings/apply':return self.reply(service.apply(body))
                        return self.reply({'error':'preview only'},code=404)
                    except Exception as error:return self.reply({'error':str(error)},code=409)

                def log_message(self,*args):pass

            with http.server.ThreadingHTTPServer(('127.0.0.1',args.port),Handler) as server:
                print('Isolated preview http://127.0.0.1:%d/settings'%args.port,flush=True)
                server.serve_forever()
        finally:core.stop()


if __name__=='__main__':main()
