"""Settings page backend: read and change SteadyRoute's settings, the AI line in Clash Verge,
the weekly rule sync and the AI routing check. Standard library only.

Nothing here runs on its own; the router calls it from the HTTP handler (settings page) and
from one background maintenance thread. One lock serialises every change to Clash.
"""

import copy
import json
import os
import pathlib
import tempfile
import threading
import time
import urllib.request

try:
    import ai_check
    import ai_line
    import ai_rules
    import auto_lock
    import clash_profile
    import regions
    import route_policy
except ModuleNotFoundError:  # pragma: no cover
    from . import ai_check, ai_line, ai_rules, auto_lock, clash_profile, regions, route_policy

HOUR = 3600
DAY = 86400
WEEK = 7 * DAY
RETRY = 6 * HOUR
EDITABLE = ("exclude_groups", "ai_line", "manual", "migration")


def _atomic_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path, default):
    try:
        with open(str(path), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def fetch_text(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": "SteadyRoute (+https://github.com/)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2 * 1024 * 1024).decode("utf-8", "replace")


def group_details(proxies, exclude):
    """The groups SteadyRoute switches (or would, if not excluded), with the country each is locked to."""
    names = auto_lock.managed_groups(proxies)
    names += [name for name in exclude if name not in names and name in proxies]
    rows = []
    for name in names:
        current = (proxies.get(name) or {}).get("now")
        code = regions.region_of(current)[0] if current and ai_line.is_node(proxies.get(current)) else None
        known = code and code != regions.OTHER_REGION[0]
        residential = sum(1 for node, proxy in proxies.items() if known and ai_line.is_node(proxy)
                          and regions.region_of(node)[0] == code and regions.is_residential(node)
                          and not regions.is_notice(node))
        status = ("excluded" if name in exclude else "unknown" if not known
                  else "no_residential" if not residential else "switching")
        rows.append({"name": name, "current": current, "country": code if known else None,
                     "country_label": regions.label(code) if known else None,
                     "residential": residential, "status": status})
    return rows


class SettingsService(object):
    def __init__(self, config_path, base_dir, controller, clash_home=None, core=None,
                 fetch=fetch_text, clock=time.time, validator=None, log=None):
        self.config_path = pathlib.Path(config_path)
        self.base_dir = pathlib.Path(base_dir)
        self.controller = controller
        self.target = clash_profile.Target(clash_home)
        self.core = core if core is not None else clash_profile.find_core()
        self.fetch = fetch
        self.clock = clock
        self.validator = validator
        self.log = log or (lambda message: None)
        self.lock = threading.Lock()
        self.applied_path = self.base_dir / "clash-applied.json"
        self.rules_path = self.base_dir / "ai-rules.json"

    # ------------------------------------------------------------ state files
    def config(self):
        return route_policy.load_policy_config(self.config_path)

    def applied(self):
        return _read_json(self.applied_path, {})

    def rules_state(self):
        return _read_json(self.rules_path, {})

    def rules_source(self):
        state = self.rules_state()
        return {key: [tuple(item) for item in state[key]] for key in ("claude", "gpt") if state.get(key)}

    def _get(self, path):
        status, body = self.controller("GET", path, None)
        if status != 200:
            raise clash_profile.ProfileError("Clash 控制接口返回 HTTP %s" % status)
        return json.loads(body or "{}")

    def proxies(self):
        return self._get("/proxies").get("proxies") or {}

    def writer(self):
        validate = self.validator
        if validate is None:
            if not self.core:
                raise clash_profile.ProfileError("没找到 Clash Verge 的内核，无法校验配置；请确认 Clash Verge 安装在“应用程序”里")
            validate = clash_profile.core_validator(self.core, self.target.home)
        return clash_profile.Writer(self.target, self.base_dir / "clash-backups", validate, self.controller)

    def manager(self, config, applied=None):
        return ai_line.Manager(config, self.applied() if applied is None else applied, self.target,
                               self.writer(), self.proxies, self.rules_source())

    # ------------------------------------------------------------ settings page
    def merged(self, changes):
        config = copy.deepcopy(self.config())
        config["policies"] = []
        unknown = [key for key in changes if key not in EDITABLE]
        if unknown:
            raise clash_profile.ProfileError("不能修改的设置项：%s" % "、".join(unknown))
        migration = changes.get("migration")
        if migration not in (None, "accept", "dismiss"):
            raise clash_profile.ProfileError("migration 只能是 accept 或 dismiss")
        suggested = config.pop("migration", None) if migration else None
        if migration == "accept" and suggested:
            # The lines an older version kept in Clash, now as auto-filtered lines (same names).
            if suggested.get("ai_line"):
                config["ai_line"] = dict(suggested["ai_line"])
            config["managed_lines"] = list(suggested.get("managed_lines") or [])
            config["legacy_group_names"] = sorted(set(config.get("legacy_group_names") or []) |
                                                  set(suggested.get("legacy_group_names") or []))
        if "exclude_groups" in changes:
            config.setdefault("auto_lock", {})["exclude_groups"] = [str(name) for name in changes["exclude_groups"]]
        if "ai_line" in changes:
            line = dict(config.get("ai_line") or {})
            line.update({key: value for key, value in changes["ai_line"].items()
                         if key in ("enabled", "country", "group_name")})
            line.setdefault("group_name", ai_line.DEFAULT_AI_GROUP)
            config["ai_line"] = line
        if "manual" in changes:
            cleaned = []
            for item in changes["manual"]:
                entry = ai_rules.manual_entry(str(item))
                if not entry:
                    raise clash_profile.ProfileError("“%s”不是有效的域名" % item)
                if entry[1] not in cleaned:
                    cleaned.append(entry[1])
            config.setdefault("ai_rules", {})["manual"] = cleaned
        route_policy.validate_policy_config(config)
        return config

    def snapshot(self):
        config = self.config()
        applied = self.applied()
        rules_state = self.rules_state()
        result = {
            "profile": config.get("profile", "fixed"),
            "exclude_groups": (config.get("auto_lock") or {}).get("exclude_groups", []),
            "ai_line": config.get("ai_line") or {"enabled": False, "group_name": ai_line.DEFAULT_AI_GROUP},
            "managed_lines": config.get("managed_lines") or [],
            "manual": (config.get("ai_rules") or {}).get("manual", []),
            "applied": {key: applied.get(key) for key in ("at", "groups", "dropped", "profile_uid")},
            "applied_rule_count": len(applied.get("rules") or []),
            "rules_source": {
                "name": "ip.net.coffee", "snapshot_date": ai_rules.SNAPSHOT_DATE,
                "synced_at": rules_state.get("synced_at"), "checked_at": rules_state.get("checked_at"),
                "next_check_at": self.next_check_at(rules_state) if rules_state.get("checked_at") else None,
                "failures": rules_state.get("failures") or 0,
                "last_error": rules_state.get("last_error"), "last_change": rules_state.get("last_change"),
                "urls": [ai_rules.NETCOFFEE_CLAUDE_URL, ai_rules.NETCOFFEE_GPT_URL],
            },
            "geo": {"updated_at": rules_state.get("geo_updated_at"), "last_error": rules_state.get("geo_error")},
            "unsupported": ai_line.AI_UNSUPPORTED,
            "migration": config.get("migration"),
            "ai_rules_counts": {
                "netcoffee": len(set(map(tuple, rules_state.get("claude") or ai_rules.NETCOFFEE_CLAUDE)) |
                                 set(map(tuple, rules_state.get("gpt") or ai_rules.NETCOFFEE_GPT))),
                "community": len(ai_rules.COMMUNITY), "processes": len(ai_rules.AI_PROCESSES),
            },
        }
        try:
            proxies = self.proxies()
            result["countries"] = ai_line.country_choices(proxies)
            result["groups"] = sorted(set(auto_lock.managed_groups(proxies)) | set(result["exclude_groups"]))
            result["group_details"] = group_details(proxies, result["exclude_groups"])
            configs = self._get("/configs")
            result["ipv6"] = configs.get("ipv6")
            result["tun"] = (configs.get("tun") or {}).get("enable")
        except Exception as error:
            result["controller_error"] = str(error)
        try:
            uid, groups_file, rules_file = self.target.resolve()
            result["clash"] = {"ok": True, "profile_uid": uid, "core": bool(self.core)}
        except clash_profile.ProfileError as error:
            result["clash"] = {"ok": False, "error": str(error), "core": bool(self.core)}
        return result

    def _needs_clash(self, old, new):
        keys = lambda config: (config.get("ai_line"), config.get("managed_lines"), (config.get("ai_rules") or {}).get("manual"))
        return keys(old) != keys(new)

    def _wants_lines(self, config):
        line = config.get("ai_line") or {}
        return bool((line.get("enabled") and line.get("country")) or config.get("managed_lines"))

    def preview(self, changes):
        new = self.merged(changes)
        old = self.config()
        if not self._needs_clash(old, new):
            return {"clash_change": False}
        if not self._wants_lines(new):
            applied = self.applied()
            return {"clash_change": True, "action": "disable",
                    "remove_groups": applied.get("groups") or [],
                    "remove_rules": len(applied.get("rules") or []),
                    "restore": len(applied.get("removed_items") or [])}
        plan = self.manager(new).plan()
        plan.pop("texts", None)
        plan.pop("removed_items", None)
        plan.pop("restored_items", None)
        plan["clash_change"] = True
        plan["action"] = "apply"
        plan["rules_sample"] = plan["rules"][:8]
        plan["rule_count"] = len(plan.pop("rules"))
        return plan

    def apply(self, changes):
        with self.lock:
            self.check_writable()
            new = self.merged(changes)
            old = self.config()
            result = {"clash_change": False}
            if self._needs_clash(old, new):
                manager = self.manager(new)
                if self._wants_lines(new):
                    plan, applied = manager.apply()
                    result = {"clash_change": True, "action": "apply", "groups": applied["groups"],
                              "rule_count": len(applied["rules"]), "dropped": applied["dropped"]}
                else:
                    applied = manager.disable()
                    result = {"clash_change": True, "action": "disable"}
                _atomic_json(self.applied_path, applied)
            self.save_config(new)
            self.log("settings changed: %s" % json.dumps(result, ensure_ascii=False))
            return result

    def remove_all(self):
        """Uninstall: take every SteadyRoute item out of Clash and put back what was there."""
        with self.lock:
            self.check_writable()
            config = copy.deepcopy(self.config())
            config["policies"] = []
            line = dict(config.get("ai_line") or {})
            line["enabled"] = False
            config["ai_line"] = line
            config["managed_lines"] = []
            applied = self.applied()
            removed = list(applied.get("groups") or [])
            if removed:
                _atomic_json(self.applied_path, self.manager(config, applied).disable())
            self.save_config(config)
            self.log("SteadyRoute lines removed from Clash: %s" % "、".join(removed))
            return removed

    def check_writable(self):
        if self.config_path.name.endswith(".default.json"):
            raise clash_profile.ProfileError("正在直接从仓库运行，设置不会保存；请先运行 install.command 安装")

    def save_config(self, config):
        self.check_writable()
        stored = copy.deepcopy(config)
        stored["policies"] = []
        route_policy.validate_policy_config(stored)
        _atomic_json(self.config_path, stored)

    # ------------------------------------------------------------ AI routing check
    def check(self):
        config = self.config()
        line = config.get("ai_line") or {}
        if not line.get("enabled") and ((config.get("migration") or {}).get("ai_line") or {}).get("enabled"):
            line = config["migration"]["ai_line"]   # before migrating: measure against the old line
        rules = self._get("/rules").get("rules") or []
        proxies = self.proxies()
        try:
            runtime = self.target.runtime.read_text(encoding="utf-8")
        except OSError:
            runtime = ""
        group = line.get("group_name") if line.get("enabled") else None
        return ai_check.run(rules, proxies, runtime, str(self.target.home), group, line.get("country") if group else None)

    # ------------------------------------------------------------ maintenance
    def next_check_at(self, state):
        return state.get("next_check_at") or (state.get("checked_at", 0) + WEEK)

    def sync(self, now, state):
        """One net.coffee sync plus a geodata update when due. Updates `state`; True if the rules changed.

        A week after a good sync; after a failed one, again in 6 hours, and daily once three in a
        row have failed, so a Mac that was offline at the wrong moment does not wait a week.
        """
        changed = False
        state["checked_at"] = int(now)
        try:
            claude = ai_rules.check_source("claude", ai_rules.parse_page(self.fetch(ai_rules.NETCOFFEE_CLAUDE_URL)),
                                           state.get("claude") or ai_rules.NETCOFFEE_CLAUDE)
            gpt = ai_rules.check_source("gpt", ai_rules.parse_page(self.fetch(ai_rules.NETCOFFEE_GPT_URL)),
                                        state.get("gpt") or ai_rules.NETCOFFEE_GPT)
            gpt = gpt if ("GEOSITE", "openai") in gpt else [("GEOSITE", "openai")] + gpt
            before = [tuple(i) for i in (state.get("claude") or ai_rules.NETCOFFEE_CLAUDE)] + \
                     [tuple(i) for i in (state.get("gpt") or ai_rules.NETCOFFEE_GPT)]
            delta = ai_rules.diff(before, claude + gpt)
            state.update({"claude": [list(i) for i in claude], "gpt": [list(i) for i in gpt],
                          "synced_at": int(now), "last_error": None, "failures": 0,
                          "next_check_at": int(now + WEEK)})
            if delta["added"] or delta["removed"]:
                state["last_change"] = {"at": int(now), "added": [list(i) for i in delta["added"]],
                                        "removed": [list(i) for i in delta["removed"]]}
                changed = True
        except Exception as error:   # keep the list in use; show the reason on the page
            failures = int(state.get("failures") or 0) + 1
            state.update({"failures": failures,
                          "next_check_at": int(now + (RETRY if failures < 3 else DAY)),
                          "last_error": "%s（%s）" % (str(error)[:200], time.strftime("%Y-%m-%d %H:%M", time.localtime(now)))})
        if now - state.get("geo_updated_at", 0) >= WEEK:
            try:
                status, body = self.controller("POST", "/configs/geo", {"path": "", "payload": ""})
                if status in (200, 204):
                    state["geo_updated_at"], state["geo_error"] = int(now), None
                else:
                    state["geo_error"] = "HTTP %s %s" % (status, str(body)[:120])
            except Exception as error:
                state["geo_error"] = str(error)[:200]
        _atomic_json(self.rules_path, state)
        return changed

    def reapply(self, config, applied, reason):
        manager = self.manager(config, applied)
        with self.lock:
            _plan, new_applied = manager.apply()
            _atomic_json(self.applied_path, new_applied)
        self.log("AI line re-applied (%s)" % reason)

    def sync_now(self):
        """The settings page's 立即同步: sync at once and write new rules into Clash if they changed."""
        self.check_writable()
        now = self.clock()
        state = self.rules_state()
        changed = self.sync(now, state)
        config, applied = self.config(), self.applied()
        if changed and self._wants_lines(config) and applied.get("groups"):
            self.reapply(config, applied, "rules updated")
        state = self.rules_state()
        return {"ok": not state.get("last_error"), "changed": changed, "error": state.get("last_error"),
                "last_change": state.get("last_change") if changed else None}

    def maintenance(self):
        """Hourly: self-heal, and the net.coffee sync when it is due. Never raises."""
        now = self.clock()
        state = self.rules_state()
        config = self.config()
        applied = self.applied()
        enabled = self._wants_lines(config) and applied.get("groups")
        changed = False
        if now >= self.next_check_at(state):
            changed = self.sync(now, state)
        if enabled and (changed or now - state.get("healed_at", 0) >= HOUR):
            try:
                manager = self.manager(config, applied)
                if changed or not manager.healthy():
                    self.reapply(config, applied, "rules updated" if changed else "not in effect")
            except Exception as error:
                self.log("AI line maintenance failed: %s" % error)
            state = self.rules_state()
            state["healed_at"] = int(now)
            _atomic_json(self.rules_path, state)
