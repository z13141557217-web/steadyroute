"""The optional AI residential line, and any other residential lines SteadyRoute keeps in Clash.

A line is a select group that includes every node of one country whose name marks it as
residential (same detection as the router, see regions.py). It never falls back to DIRECT:
Mihomo's default for an empty group is COMPATIBLE, which is a direct connection, so the group
sets empty-fallback: REJECT. UDP stays on (net.coffee: "确保你的配置开启了 UDP 代理"), so NTP,
QUIC and WebRTC leave through the same residential exit instead of being refused or leaking through
another exit, and gets the AI rules (ai_rules.py) in front of every rule of the user's own.

plan() computes everything without touching a file; apply() / disable() go through
clash_profile.Writer (backup → core validation → write → reload → verify → rollback).
"""

import json
import re
import time

try:
    import ai_rules
    import clash_profile
    import regions
except ModuleNotFoundError:  # pragma: no cover
    from . import ai_rules, clash_profile, regions

DEFAULT_AI_GROUP = "AI 家宽专线"
# Checked 2026-09-30 against https://www.anthropic.com/supported-countries and
# https://help.openai.com/en/articles/7947663 : neither serves these regions.
AI_UNSUPPORTED = {"HK": "香港", "MO": "澳门", "RU": "俄罗斯", "CN": "中国大陆"}
AI_BUSINESS_URLS = [
    "https://chatgpt.com/cdn-cgi/trace",
    "https://api.openai.com/cdn-cgi/trace",
    "https://claude.ai/cdn-cgi/trace",
    "https://api.anthropic.com/v1/models",
]


def business_urls(existing=None):
    """The AI line always tests web and API separately; URLs kept from an older setup come after."""
    urls = list(AI_BUSINESS_URLS)
    return urls + [url for url in existing or [] if url not in urls]


EXCLUDE_FILTER = "(?i)" + regions.INFO_RE.pattern


def country_pattern(code):
    for region_code, _label, pattern in regions.REGIONS:
        if region_code == code:
            return pattern
    raise ValueError("unknown country %s" % code)


def line_filter(code):
    """Mihomo filter (regexp2, lookarounds allowed): this country AND residential."""
    return "(?i)^(?=.*(?:%s))(?=.*(?:%s))" % (country_pattern(code), regions.RESIDENTIAL_RE.pattern)


def members(code, node_names):
    """What Mihomo will put in the group: the same regexes, evaluated here for the preview."""
    include = re.compile(line_filter(code))
    exclude = re.compile(EXCLUDE_FILTER)
    return [name for name in node_names if include.search(name) and not exclude.search(name)]


def group_definition(name, code, ai):
    group = {
        "name": name, "type": "select", "include-all-proxies": True,
        "filter": line_filter(code), "exclude-filter": EXCLUDE_FILTER, "exclude-type": "direct",
        "empty-fallback": "REJECT", "interrupt-exist-connections": False,
    }
    return group


def is_node(proxy):
    kind = str((proxy or {}).get("type") or "").replace("-", "").lower()
    return bool(kind) and "all" not in (proxy or {}) and kind not in regions.NON_NODE_TYPES


def country_choices(proxy_data):
    """[{code, label, residential, ai_ok}] for countries with residential nodes, most first."""
    counts = {}
    for name, proxy in (proxy_data or {}).items():
        if is_node(proxy) and regions.is_residential(name) and not regions.is_notice(name):
            code = regions.region_of(name)[0]
            if code != regions.OTHER_REGION[0]:
                counts[code] = counts.get(code, 0) + 1
    rows = [{"code": code, "label": regions.label(code), "residential": count,
             "ai_ok": code not in AI_UNSUPPORTED} for code, count in counts.items()]
    return sorted(rows, key=lambda row: (not row["ai_ok"], -row["residential"], row["code"]))


class Manager(object):
    """Settings live in the router config: ai_line, managed_lines, ai_rules.manual."""

    def __init__(self, config, applied, target, writer, proxies_reader, rules_source=None):
        self.config = config
        self.applied = applied            # dict persisted by the caller (what we wrote last)
        self.target = target
        self.writer = writer
        self.proxies_reader = proxies_reader
        self.rules_source = rules_source or {}  # {"claude": [...], "gpt": [...]} synced lists

    # ------------------------------------------------------------ what we want in Clash
    def lines(self):
        wanted = []
        ai = self.config.get("ai_line") or {}
        if ai.get("enabled") and ai.get("country"):
            wanted.append({"name": ai.get("group_name") or DEFAULT_AI_GROUP, "country": ai["country"], "ai": True})
        for line in self.config.get("managed_lines") or []:
            wanted.append({"name": line["group_name"], "country": line["country"], "ai": False})
        return wanted

    def rule_lines(self, drop=()):
        ai = self.config.get("ai_line") or {}
        if not (ai.get("enabled") and ai.get("country")):
            return [], []
        manual = [ai_rules.manual_entry(item) for item in (self.config.get("ai_rules") or {}).get("manual", [])]
        return ai_rules.build(ai.get("group_name") or DEFAULT_AI_GROUP,
                              claude=self.rules_source.get("claude"), gpt=self.rules_source.get("gpt"),
                              manual=[item for item in manual if item], drop=drop)

    def plan(self, drop=()):
        """Everything apply() would write, plus a human preview. Touches nothing."""
        uid, groups_file, rules_file = self.target.resolve()
        lines = self.lines()
        rule_lines, breakdown = self.rule_lines(drop)
        names = {line["name"] for line in lines}
        legacy = set(self.config.get("legacy_group_names") or []) | set(self.applied.get("groups") or [])
        groups = [group_definition(line["name"], line["country"], line["ai"]) for line in lines]
        group_items = [clash_profile.group_item(group) for group in groups]
        rule_items = [clash_profile.rule_item(line) for line in rule_lines]
        groups_text = groups_file.read_text(encoding="utf-8")
        rules_text = rules_file.read_text(encoding="utf-8")
        new_groups, removed_groups = clash_profile.edit_prepend(groups_text, group_items, names | legacy)
        # A line we no longer keep but that replaced a group of the same name (e.g. the AI line
        # switched off after migrating): put the old definition back, since the user's own rules
        # may still point at that name. Everything else stays until disable() restores it all.
        gone = set(self.applied.get("groups") or []) - names
        restored = [item for item in self.applied.get("removed_items") or []
                    if clash_profile._item_name(item.splitlines()[0]) in gone]
        new_groups = clash_profile.insert_prepend_items(new_groups, restored)
        new_rules, _removed_rules = clash_profile.edit_prepend(rules_text, rule_items)
        runtime = self.target.runtime.read_text(encoding="utf-8")
        new_runtime = clash_profile.patch_runtime(
            runtime, group_items, rule_items, names | legacy, stale_rules=self.applied.get("rules") or [])
        new_runtime = insert_runtime_groups(new_runtime, [dedent_item(item) for item in restored])
        proxy_data = self.proxies_reader()
        nodes = [name for name, proxy in proxy_data.items() if is_node(proxy)]
        preview = []
        for line in lines:
            before = (proxy_data.get(line["name"]) or {}).get("all")
            after = members(line["country"], nodes)
            preview.append({
                "group": line["name"], "country": line["country"], "country_label": regions.label(line["country"]),
                "ai": line["ai"], "exists": before is not None,
                "before": [n for n in (before or []) if n in proxy_data and is_node(proxy_data[n])],
                "after": after,
                "added": [n for n in after if n not in (before or [])],
                "removed": [n for n in (before or []) if n not in after and is_node(proxy_data.get(n))],
                "ai_unsupported": line["ai"] and line["country"] in AI_UNSUPPORTED,
            })
        return {
            "profile_uid": uid, "groups_file": groups_file.name, "rules_file": rules_file.name,
            "lines": preview, "rules": rule_lines, "rule_breakdown": breakdown, "groups": groups,
            "removed_rules": [line for line in self.applied.get("rules") or [] if line not in rule_lines],
            "removed_legacy": [clash_profile._item_name(item.splitlines()[0]) for item in removed_groups],
            "texts": {"groups": new_groups, "rules": new_rules, "runtime": new_runtime},
            "removed_items": removed_groups, "dropped": [list(item) for item in drop],
            "restored_items": restored,
            "restored": [clash_profile._item_name(item.splitlines()[0]) for item in restored],
        }

    # ------------------------------------------------------------ changing Clash
    def validated_plan(self):
        """plan() as it will be written: checked by the Clash core, optional rules it cannot load dropped."""
        for line in self.lines():
            if line["ai"] and line["country"] in AI_UNSUPPORTED:
                raise clash_profile.ProfileError("%s不在 ChatGPT / Claude 的服务地区内，不能用作 AI 专线" % (
                    AI_UNSUPPORTED[line["country"]]))
        drop = []
        optional = [item for item in ai_rules.OPTIONAL]
        while True:
            plan = self.plan(drop)
            try:
                self.writer.validate(plan["texts"]["runtime"])
                break
            except clash_profile.ProfileError as error:
                remaining = [item for item in optional if item not in drop and any(
                    item[1] in rule for rule in plan["rules"])]
                if not remaining:
                    raise
                # e.g. no category-ai-!cn in an old geosite database, or no ASN database:
                # drop the one the core complained about, else the first optional rule.
                text = str(error).lower()
                named = [item for item in remaining if item[1].lower() in text
                         or (item[0] == "IP-ASN" and "asn" in text)]
                drop.append((named or remaining)[0])
        return plan

    def apply(self):
        plan = self.validated_plan()
        names = [line["name"] for line in self.lines()]
        first_rules = plan["rules"][:3]

        def verify():
            data = self.proxies_reader()
            missing = [name for name in names if name not in data]
            if missing:
                raise clash_profile.ProfileError("重新加载后没有看到分组：%s" % "、".join(missing))

        backup = self.writer.commit(
            plan["texts"]["groups"], plan["texts"]["rules"], plan["texts"]["runtime"],
            {"action": "apply", "at": int(time.time()), "profile_uid": plan["profile_uid"],
             "removed_items": plan["removed_items"], "previous_applied": self.applied},
            verify=verify, keep_selection=names)
        removed = [item for item in self.applied.get("removed_items") or []
                   if item not in plan["restored_items"]] + plan["removed_items"]
        # Keep the original order of the groups we took out, so disable() puts them back as they were.
        order = list(self.applied.get("removed_order") or [])
        for item in removed:
            name = clash_profile._item_name(item.splitlines()[0])
            if name not in order:
                order.append(name)
        removed.sort(key=lambda item: order.index(clash_profile._item_name(item.splitlines()[0])))
        self.applied = {
            "profile_uid": plan["profile_uid"], "groups": names, "rules": plan["rules"],
            "removed_items": removed, "removed_order": order, "dropped": plan["dropped"], "at": int(time.time()),
            "backup": str(backup), "first_rules": first_rules,
        }
        return plan, self.applied

    def disable(self):
        """Take every SteadyRoute item out of Clash and put back what an older version had there."""
        uid, groups_file, rules_file = self.target.resolve()
        removed = self.applied.get("removed_items") or []
        groups_text = clash_profile.restore_prepend(groups_file.read_text(encoding="utf-8"), removed)
        rules_text = clash_profile.restore_prepend(rules_file.read_text(encoding="utf-8"), [])
        restored_runtime_groups = [dedent_item(item) for item in removed]
        runtime = clash_profile.patch_runtime(
            self.target.runtime.read_text(encoding="utf-8"), [], [],
            set(self.applied.get("groups") or []), stale_rules=self.applied.get("rules") or [])
        runtime = insert_runtime_groups(runtime, restored_runtime_groups)
        self.writer.commit(groups_text, rules_text, runtime,
                           {"action": "disable", "at": int(time.time()), "profile_uid": uid,
                            "previous_applied": self.applied})
        self.applied = {}
        return self.applied

    def healthy(self, runtime_text=None, rules_payload=None):
        """Is what we applied still in effect (e.g. after the user switched subscriptions)?"""
        if not self.applied.get("groups"):
            return True
        try:
            uid, _g, _r = self.target.resolve()
        except clash_profile.ProfileError:
            return True
        if uid != self.applied.get("profile_uid"):
            return False
        text = runtime_text if runtime_text is not None else self.target.runtime.read_text(encoding="utf-8")
        return all(json.dumps(line, ensure_ascii=False) in text or line in text
                   for line in (self.applied.get("first_rules") or [])[:1])


def dedent_item(item):
    lines = item.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    return "\n".join(line[indent:] if line[:indent].strip() == "" else line for line in lines)


def insert_runtime_groups(text, items):
    if not items:
        return text
    lines = text.splitlines()
    at = clash_profile._find_key(lines, "proxy-groups")
    indent, _end = clash_profile._sequence(lines, at)
    pad = " " * (indent or 0)
    block = []
    for item in items:
        block.extend(pad + line for line in item.splitlines())
    return "\n".join(lines[:at + 1] + block + lines[at + 1:]) + "\n"
