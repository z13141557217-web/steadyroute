#!/usr/bin/env python3
"""Stand-in for verge-mihomo -t: parses the staged config and checks references like the core does."""
import re
import sys

import yaml

args = sys.argv[1:]
path = args[args.index("-f") + 1]
try:
    config = yaml.safe_load(open(path, encoding="utf-8"))
    names = {p["name"] for p in config.get("proxies") or []}
    groups = [g["name"] for g in config.get("proxy-groups") or []]
    if len(groups) != len(set(groups)):
        raise ValueError("duplicate proxy group name")
    known = names | set(groups) | {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
    for group in config.get("proxy-groups") or []:
        for member in group.get("proxies") or []:
            if member not in known:
                raise ValueError("proxy group[%s]: '%s' not found" % (group["name"], member))
        for key in ("filter", "exclude-filter"):
            if group.get(key):
                re.compile(group[key])
    providers = set((config.get("rule-providers") or {}).keys())
    for index, rule in enumerate(config.get("rules") or []):
        rule = str(rule)
        if rule.startswith(("AND,", "OR,", "NOT,")):
            continue
        parts = rule.split(",")
        target = parts[1] if parts[0] == "MATCH" else parts[2]
        if target not in known:
            raise ValueError("rules[%d] [%s] error: proxy [%s] not found" % (index, rule, target))
        if parts[0] == "RULE-SET" and parts[1] not in providers:
            raise ValueError("rules[%d] [%s] error: rule set [%s] not found" % (index, rule, parts[1]))
except Exception as error:
    print("level=error msg=\"%s\"" % error)
    print("configuration file %s test failed" % path)
    sys.exit(1)
print("configuration file %s test is successful" % path)
