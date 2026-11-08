#!/usr/bin/env python3
"""Read-only synthetic node table preview. Never connects to the production controller."""
import argparse
import http.server
import json
import pathlib
import sys
import time
from urllib.parse import parse_qs, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import cycle_harness

router = cycle_harness.load_router("node_table_preview")


def payload():
    now = time.time()
    state = router.state_contract.new_state()
    net = cycle_harness.FakeNetwork(router)
    for policy in router.POLICIES:
        for index, name in enumerate(policy["static_candidates"]):
            n = cycle_harness.mature_node(80 + index * 25, now)
            n.update(win_n=40, win_success=max(0, 1-index*.12), win_p90=120,
                     last_success_delay=140, last_success_at=int(now)-9300)
            n["last_probe_result"] = dict(status="success", delay_ms=110,
                observed_at=now-5, reason_code=None)
            if index in (1, 2, 3):
                n.update(pause_need=20, success_streak=3 if index == 1 else 0,
                         failure_streak=0 if index == 1 else 39, last_success=index == 1)
            if index in (2, 3, 4):
                n["last_probe_result"].update(status={2:"timeout",3:"error",4:"unavailable"}[index],
                    delay_ms=None, reason_code="probe_timeout" if index==2 else "unknown")
            if index == 5:
                n["last_probe_result"]["observed_at"] = now-1000
            if index == 6:
                n.pop("last_probe_result")
            state["nodes"][name] = n
        state["groups"][policy["group_name"]] = {"hot_standby":policy["static_candidates"][1]}
    state.update(updated_at=now, controller_connected=True)
    result = router.build_status_snapshots(state, net.proxy_data, [], now=now)["legacy"]
    old = router.POLICIES[0]["static_candidates"][3]
    long_name = "【示例倍率】中转专线｜台湾家用宽带备用线路｜用于验证长名称两行截断与完整详情展示04"
    result["nodes"][long_name] = result["nodes"].pop(old)
    for group in result["groups"]:
        group["candidates"] = [long_name if name == old else name for name in group["candidates"]]
    return result


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        location = urlsplit(self.path)
        path = location.path
        if path == "/api/status":
            body = json.dumps(payload(),ensure_ascii=False).encode()
            mime = "application/json"
        else:
            names = {"/":"dashboard.html", "/changelog":"changelog.html",
                     "/guide":"guide.html", "/assets/pages.css":"pages.css"}
            if path not in names:
                self.send_error(404, "Synthetic preview only")
                return
            body = (ROOT/"src/steadyroute"/names[path]).read_bytes()
            mime = "text/css" if path.endswith(".css") else "text/html"
            if path == "/":
                query = parse_qs(location.query)
                design = "comfortable" if query.get("design") == ["comfortable"] else "compact"
                theme = "dark" if query.get("theme") == ["dark"] else "light"
                body = body.replace(b'<html lang="zh-CN">', ('<html lang="zh-CN" data-theme="%s" data-node-design="%s">' % (theme, design)).encode())
                choices = [
                    ("compact", theme, "A 紧凑监控"), ("comfortable", theme, "B 舒适清单"),
                    (design, "light", "浅色"), (design, "dark", "深色"),
                ]
                links = ''.join('<a href="/?design=%s&amp;theme=%s#node-panel" style="color:var(--accent);padding:6px 8px;border-radius:5px;%s">%s</a>' % (
                    d, t, 'background:var(--accent-soft);font-weight:600;' if d==design and t==theme else '', label)
                    for d,t,label in choices)
                controls = '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:12px 24px;border-bottom:1px solid var(--hair);font-size:12px;color:var(--ink-2)"><span>设计对比 · 模拟数据</span>%s</div>' % links
                body = body.replace(b'<div class="nodes-head">', controls.encode()+b'<div class="nodes-head">', 1)
                # Focus the review on the requested component, without hiding anything in production.
                body = body.replace(b"</head>", b"<style>main.wrap > .head,main.wrap > .routes,main.wrap > .idle{display:none}</style></head>")
        self.send_response(200)
        self.send_header("Content-Type",mime+"; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):
            pass

    def do_POST(self):
        self.send_error(405,"Read-only preview")

    def log_message(self,*args):
        pass


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=18771)
    args=parser.parse_args()
    with http.server.ThreadingHTTPServer(("127.0.0.1",args.port),Handler) as server:
        print("Synthetic preview http://127.0.0.1:%d/"%args.port,flush=True)
        server.serve_forever()
