"""Fake Clash Verge for the v0.5.1 end-to-end run: a Mihomo controller on a Unix socket whose
groups, rules and members are computed from the runtime clash-verge.yaml, reloaded on PUT /configs.
"""

import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import fake_mihomo  # noqa: E402

TYPE_NAMES = {
    "DOMAIN": "Domain", "DOMAIN-SUFFIX": "DomainSuffix", "DOMAIN-KEYWORD": "DomainKeyword",
    "IP-CIDR": "IPCIDR", "IP-CIDR6": "IPCIDR6", "IP-ASN": "IPASN", "GEOSITE": "GeoSite", "GEOIP": "GeoIP",
    "RULE-SET": "RuleSet", "PROCESS-NAME": "ProcessName", "MATCH": "Match", "AND": "AND", "OR": "OR",
}
GROUP_TYPES = {"select": "Selector", "url-test": "URLTest", "fallback": "Fallback", "load-balance": "LoadBalance"}


def parse_rule(line):
    head, _, rest = line.partition(",")
    if head in ("AND", "OR", "NOT"):
        depth = 0
        for index, char in enumerate(rest):
            depth += char == "("
            depth -= char == ")"
            if depth == 0:
                payload, target = rest[:index + 1], rest[index + 2:].split(",")[0]
                return {"type": head, "payload": payload, "proxy": target}
    parts = line.split(",")
    if parts[0] == "MATCH":
        return {"type": "Match", "payload": "", "proxy": parts[1]}
    return {"type": TYPE_NAMES.get(parts[0], parts[0]), "payload": parts[1], "proxy": parts[2]}


def members(group, nodes):
    found = list(group.get("proxies") or [])
    if group.get("include-all-proxies") or group.get("include-all"):
        candidates = nodes
        if group.get("filter"):
            candidates = [n for n in candidates if any(re.search(p, n) for p in group["filter"].split("`"))]
        if group.get("exclude-filter"):
            candidates = [n for n in candidates if not re.search(group["exclude-filter"], n)]
        found += [n for n in candidates if n not in found]
    return found or [group.get("empty-fallback", "COMPATIBLE")]


class Clash(fake_mihomo.World):
    def __init__(self, runtime_path, latency):
        super().__init__({}, latency, {name: 6 for name in latency})
        self.runtime_path = runtime_path
        self.types = {}
        self.hidden = set()
        self.rules = []
        self.reloads = 0
        self.geo_updates = 0
        self.load(runtime_path)

    def load(self, path):
        config = yaml.safe_load(open(path, encoding="utf-8"))
        nodes = [p["name"] for p in config.get("proxies") or []]
        groups, types, hidden = {}, {}, set()
        for group in config.get("proxy-groups") or []:
            all_ = members(group, nodes)
            previous = self.groups.get(group["name"], {}).get("now")
            groups[group["name"]] = {"now": previous if previous in all_ else all_[0], "all": all_}
            types[group["name"]] = GROUP_TYPES.get(group["type"], "Selector")
            if group.get("hidden"):
                hidden.add(group["name"])
        with self.lock:
            self.groups, self.types, self.hidden = groups, types, hidden
            for name in nodes:
                self.latency.setdefault(name, 150)
            self.rules = [parse_rule(str(line)) for line in config.get("rules") or []]
            self.reloads += 1
        self.record("reload", path=path, groups=len(groups), rules=len(self.rules))

    def proxies_payload(self):
        payload = super().proxies_payload()
        for name, item in payload["proxies"].items():
            if name in self.types:
                item["type"] = self.types[name]
                if name in self.hidden:
                    item["hidden"] = True
        payload["proxies"]["REJECT"] = {"name": "REJECT", "type": "Reject"}
        payload["proxies"]["COMPATIBLE"] = {"name": "COMPATIBLE", "type": "Compatible"}
        return payload


def make_handler(clash):
    base = fake_mihomo.make_handler(clash)

    class Handler(base):
        def read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            route = unquote(urlsplit(self.path).path)
            if route == "/version":
                return self.reply(200, {"meta": True, "version": "v1.19.31"})
            if route == "/rules":
                return self.reply(200, {"rules": clash.rules})
            if route == "/configs":
                return self.reply(200, {"ipv6": False, "mode": "rule", "tun": {"enable": True}})
            return base.do_GET(self)

        def do_PUT(self):
            route = unquote(urlsplit(self.path).path)
            if route == "/configs":
                payload = self.read_json()
                try:
                    clash.load(payload["path"])
                except Exception as error:
                    return self.reply(400, {"message": str(error)})
                return self.reply(204, None)
            return base.do_PUT(self)

        def do_POST(self):
            route = unquote(urlsplit(self.path).path)
            self.read_json()
            if route == "/configs/geo":
                clash.geo_updates += 1
                clash.record("geo")
                return self.reply(204, None)
            return self.reply(404, {"message": "not found"})

    return Handler


def serve(clash, socket_path):
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = fake_mihomo.UnixServer(socket_path, make_handler(clash))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
