#!/usr/bin/env python3
"""A scriptable fake Mihomo controller on a Unix socket, for local acceptance runs.

It serves the endpoints SteadyRoute uses (/proxies, /connections, delay tests,
group selection and connection close) and lets a scenario break nodes, AI
sites, or the whole local network at chosen times. Test-only: never ship it.
"""

import http.server
import json
import os
import random
import socketserver
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit


class World(object):
    """Mutable network conditions shared by the controller threads."""

    def __init__(self, groups, latency, jitter, seed=7):
        self.lock = threading.Lock()
        self.groups = {name: dict(item) for name, item in groups.items()}
        self.latency = dict(latency)
        self.jitter = dict(jitter)
        self.rng = random.Random(seed)
        self.down_nodes = set()
        self.flaky = {}               # node -> failure probability
        self.site_down = set()        # URLs failing through every node
        self.local_offline = False
        self.blackout_until = 0.0     # every proxy probe fails (e.g. just after wake)
        self.connections = {}         # id -> {"group", "node"}
        self.log = []                 # controller-side actions, for the report
        self.started = time.time()
        self.connection_seq = 0

    def rel(self):
        return round(time.time() - self.started, 2)

    def record(self, kind, **fields):
        entry = {"t": self.rel(), "kind": kind}
        entry.update(fields)
        self.log.append(entry)

    def open_stream(self, group):
        """Model one long-lived AI streaming connection on the group's current node."""
        with self.lock:
            self.connection_seq += 1
            cid = "conn-%d" % self.connection_seq
            self.connections[cid] = {"group": group, "node": self.groups[group]["now"]}
        return cid

    def delay(self, name, url, timeout_ms):
        with self.lock:
            now = time.time()
            if self.local_offline:
                return None
            if name == "DIRECT":
                return 25
            if now < self.blackout_until:
                return None
            if name in self.down_nodes or url in self.site_down:
                return None
            if self.rng.random() < self.flaky.get(name, 0.0):
                return None
            base = float(self.latency.get(name, 120))
            spread = float(self.jitter.get(name, 5))
            value = max(5.0, self.rng.gauss(base, spread / 1.5))
            if "cdn-cgi/trace" in url:
                value *= 1.6
            elif url.startswith("http://"):
                value *= 0.6          # plain HTTP skips the TLS handshake
            return int(value) if value < timeout_ms else None

    def proxies_payload(self):
        with self.lock:
            proxies = {"DIRECT": {"name": "DIRECT", "type": "Direct"}}
            for name in self.latency:
                proxies[name] = {"name": name, "type": "Hysteria2"}
            for name, item in self.groups.items():
                proxies[name] = {"name": name, "type": "Selector", "now": item["now"], "all": list(item["all"])}
            return {"proxies": proxies}

    def connections_payload(self):
        with self.lock:
            return {"connections": [
                {"id": cid, "chains": [item["node"], item["group"]], "upload": 1, "download": 1}
                for cid, item in self.connections.items()
            ]}


def make_handler(world):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            return

        def address_string(self):
            return "unix"

        def reply(self, status, payload):
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            parts = urlsplit(self.path)
            route = unquote(parts.path)
            if route == "/proxies":
                return self.reply(200, world.proxies_payload())
            if route == "/connections":
                return self.reply(200, world.connections_payload())
            if route.startswith("/proxies/") and route.endswith("/delay"):
                name = route[len("/proxies/"):-len("/delay")]
                query = parse_qs(parts.query)
                timeout_ms = int(query.get("timeout", ["5000"])[0])
                url = query.get("url", [""])[0]
                started = time.time()
                delay = world.delay(name, url, timeout_ms)
                if delay is None:
                    time.sleep(timeout_ms / 1000.0)
                    world.record("probe", node=name, url=url, ok=False,
                                 started=round(started - world.started, 2))
                    return self.reply(504, {"message": "Timeout"})
                time.sleep(delay / 1000.0)
                world.record("probe", node=name, url=url, ok=True, delay=delay,
                             started=round(started - world.started, 2))
                return self.reply(200, {"delay": delay})
            return self.reply(404, {"message": "not found"})

        def do_PUT(self):
            route = unquote(urlsplit(self.path).path)
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            group = route[len("/proxies/"):]
            with world.lock:
                if group not in world.groups or payload.get("name") not in world.groups[group]["all"]:
                    return self.reply(400, {"message": "bad selection"})
                previous = world.groups[group]["now"]
                world.groups[group]["now"] = payload["name"]
            world.record("select", group=group, node=payload["name"], previous=previous)
            return self.reply(204, None)

        def do_DELETE(self):
            route = unquote(urlsplit(self.path).path)
            cid = route[len("/connections/"):]
            with world.lock:
                item = world.connections.pop(cid, None)
            if item:
                world.record("close", group=item["group"], node=item["node"], connection=cid)
            return self.reply(204, None)

    return Handler


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(world, socket_path):
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = UnixServer(socket_path, make_handler(world))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
