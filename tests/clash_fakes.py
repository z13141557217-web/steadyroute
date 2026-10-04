"""What the fake controllers in the tests share: a core that runs the config it was last handed."""

import json
import pathlib

import clash_profile

FIXTURE_RUNTIME = pathlib.Path(__file__).parent / "fixtures" / "clash_verge" / "clash-verge.yaml"
# how Mihomo names rule types in GET /rules
TYPE_NAMES = {"DOMAIN": "Domain", "DOMAIN-SUFFIX": "DomainSuffix", "DOMAIN-KEYWORD": "DomainKeyword",
              "IP-CIDR": "IPCIDR", "IP-CIDR6": "IPCIDR", "IP-ASN": "IPASN", "GEOSITE": "GeoSite", "GEOIP": "GeoIP",
              "PROCESS-NAME": "ProcessName", "RULE-SET": "RuleSet", "MATCH": "Match"}


class LoadedConfig(object):
    """Tracks PUT /configs like Mihomo does: text in "payload", or a path under the core's home."""

    def __init__(self, text=None, home=None):
        self.text = FIXTURE_RUNTIME.read_text(encoding="utf-8") if text is None else text
        self.home = home          # None: any path is accepted (the core's home is Clash Verge's folder)
        self.reloads = []

    def put(self, payload):
        """(status, body) of PUT /configs."""
        payload = payload or {}
        if payload.get("payload"):
            self.text = payload["payload"]
            self.reloads.append("payload")
            return 204, ""
        path = payload.get("path") or ""
        if self.home is not None and not path.startswith(str(self.home).rstrip("/") + "/"):
            return 400, json.dumps({"message": "path is not subpath of home directory or SAFE_PATHS: %s \n "
                                               "allowed paths: [%s]" % (path, self.home)})
        self.text = pathlib.Path(path).read_text(encoding="utf-8")
        self.reloads.append("path")
        return 204, ""

    def groups(self, data, default):
        """`data` (a /proxies map) with exactly the groups of the loaded config."""
        names = clash_profile.runtime_group_names(self.text) or []
        for name in [n for n, p in data.items() if p.get("type") in clash_profile.GROUP_TYPES and n not in names]:
            del data[name]
        for name in names:
            data.setdefault(name, dict(default))
        return data

    def rules(self):
        """GET /rules for the loaded config: [{type, payload, proxy}] in order."""
        lines = self.text.splitlines()
        at = clash_profile._find_key(lines, "rules")
        if at is None:
            return []
        indent, end = clash_profile._sequence(lines, at)
        found = []
        for first, _last in clash_profile._split_items(lines, at + 1, end, indent or 0):
            item = clash_profile._normal_rule(lines[first].strip()[2:])
            kind, _, rest = item.partition(",")
            if kind in ("AND", "OR", "NOT"):
                found.append({"type": kind, "payload": rest.rsplit(",", 1)[0], "proxy": rest.rsplit(",", 1)[1]})
            elif kind == "MATCH":
                found.append({"type": "Match", "payload": "", "proxy": rest})
            else:
                parts = rest.split(",")
                found.append({"type": TYPE_NAMES.get(kind, kind), "payload": parts[0], "proxy": parts[1]})
        return found
