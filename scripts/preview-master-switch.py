#!/usr/bin/env python3
"""Synthetic isolated UI; no installed state, no real subscription."""
import argparse
import json
import os
import pathlib
import sys
import tempfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
import cycle_harness
import real_core
import service_control
import settings_service
from test_real_core import GEOSITE, BASE_CONFIG


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18786)
    args=parser.parse_args()
    if not real_core.available():parser.error('set STEADYROUTE_MIHOMO to an isolated core')
    core=real_core.Core(GEOSITE)
    with tempfile.TemporaryDirectory(prefix='sr-control-preview-') as td:
        base=pathlib.Path(td);config=base/'config.json';config.write_text(json.dumps(BASE_CONFIG))
        service=settings_service.SettingsService(config,base,core,clash_home=core.verge,core=real_core.CORE)
        group='AI 台湾家宽线路';target=core.proxies()[group]['now']
        service.apply({'ai_line':dict(enabled=True,country='TW',group_name=group,exit_policy='locked',locked_node=target)})
        uid=service.target.resolve()[0]
        (core.verge/'profiles'/(uid+'.yaml')).write_text(json.dumps({'proxies':[
            dict(name=n,type='socks5',server='127.0.0.1',port=9) for n in real_core.NODES]}))
        os.environ.update(STEADYROUTE_CONTROLLER_SOCKET=core.socket,STEADYROUTE_CLASH_HOME=str(core.verge),
                          STEADYROUTE_LOG_DIR=str(base/'logs'))
        router=cycle_harness.load_router('control_preview_router')
        router.BASE_DIR=str(base);router.POLICY_CONFIG_PATH=str(config)
        control=service_control.Control(base,service)
        control.save(actual='paused',desired_running=False)
        handler=service_control.make_handler(control,router,synthetic=True)
        server=router.QuietHTTPServer(('127.0.0.1',args.port),handler)
        print('Synthetic preview: http://127.0.0.1:%d/control'%args.port,flush=True)
        try:server.serve_forever()
        finally:
            if control.worker:control.pause()
            server.server_close();core.stop()


if __name__=='__main__':main()
