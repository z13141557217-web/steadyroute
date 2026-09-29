"""Auto-lock profile for shared installs.

A friend's Clash Verge keeps its own groups and rules. SteadyRoute takes over each select
group that points straight at a node, locks it to that node's country, and from then on only
switches among that country's residential nodes inside the same group. Nothing in the
friend's Clash configuration is changed.

Rules (issue #32):
- Never cross countries on our own. Candidates are the group's members whose name is detected
  as the locked country AND residential (家宽 / 住宅 / ISP / Residential ...).
- The user switching the group to another country is a change of mind: lock the new country.
- A country without residential members is monitored only.
- A group whose current node's country cannot be detected is left alone until it can.

Every cycle rebuilds plain policy dicts in the same shape as config/route-policies.json, so
the existing health model, failover, optimisation and the three same-region guards apply
unchanged (route_policy.name_matches understands match == "country_residential").
"""

import hashlib

try:
    import regions
    import route_policy
except ModuleNotFoundError:  # pragma: no cover - imported as a package in some tools
    from . import regions
    from . import route_policy


DEFAULT_BUSINESS_TEST_URLS = ("https://www.gstatic.com/generate_204",)
SKIP_GROUPS = {"GLOBAL"}
NON_NODE_TYPES = {
    "selector", "urltest", "fallback", "loadbalance", "relay", "compatible", "pass",
    "reject", "rejectdrop", "direct", "dns",
}


def is_node(proxy):
    """A real proxy node (not a group, not DIRECT/REJECT)."""
    if not isinstance(proxy, dict) or isinstance(proxy.get("all"), list):
        return False
    kind = str(proxy.get("type") or "").replace("-", "").lower()
    return bool(kind) and kind not in NON_NODE_TYPES


def managed_groups(proxy_data, exclude=()):
    """Select groups that currently point straight at a node, in subscription order.

    Skips GLOBAL, hidden groups, url-test / fallback groups (Clash picks their node itself),
    groups that select DIRECT / REJECT, and groups that select another group (switching the
    inner group's node is enough).
    """
    excluded = set(exclude) | SKIP_GROUPS
    found = []
    for name, group in (proxy_data or {}).items():
        if not isinstance(name, str) or name in excluded or not isinstance(group, dict):
            continue
        if str(group.get("type") or "").lower() != "selector" or group.get("hidden"):
            continue
        current = group.get("now")
        if current in route_policy.BUILTIN_CANDIDATES or not is_node(proxy_data.get(current)):
            continue
        found.append(name)
    return found


def _policy_id(group_name, country):
    """One registry record per (group, country): a relock starts from a clean candidate set."""
    key = "%s\0%s" % (group_name, country)
    return "auto-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def prune_registry(state, policies):
    """Drop registry records of auto policies that no longer exist (relocked or group gone)."""
    records = (state.get("candidate_registry") or {}).get("policies") or {}
    live = {policy["id"] for policy in policies}
    for policy_id in [key for key in records if key.startswith("auto-") and key not in live]:
        del records[policy_id]


NODE_RETAIN_SECONDS = 86400


def prune_nodes(state, proxy_data, now):
    """Forget stats of nodes that left the subscription more than a day ago."""
    nodes = state.get("nodes") or {}
    for name in [name for name, stats in nodes.items()
                 if name not in (proxy_data or {})
                 and now - int((stats or {}).get("last_probe_at", 0)) > NODE_RETAIN_SECONDS]:
        del nodes[name]


def update_lock(locks, group_name, current, router_choice, now):
    """Return (lock, event) after looking at the node the group is on right now.

    event is None, "locked" (first time) or "relocked" (the user moved to another country).
    """
    country = regions.region_of(current)[0] if isinstance(current, str) else regions.OTHER_REGION[0]
    known = country != regions.OTHER_REGION[0]
    lock = locks.get(group_name)
    if lock is None:
        if not known:
            return None, None
        lock = {"country": country, "locked_at": int(now), "reason": "detected"}
        locks[group_name] = lock
        return lock, "locked"
    if known and country != lock["country"] and current != router_choice:
        lock = {"country": country, "locked_at": int(now), "reason": "manual_change", "previous": lock["country"]}
        locks[group_name] = lock
        return lock, "relocked"
    return lock, None


def build_policies(proxy_data, state, now, settings=None):
    """Runtime policies for this cycle, plus a per-group status for the dashboard.

    Returns (policies, statuses, events). Mutates state["auto_lock"] (the persisted locks).
    """
    settings = settings or {}
    urls = list(settings.get("business_test_urls") or DEFAULT_BUSINESS_TEST_URLS)
    locks = state.setdefault("auto_lock", {})
    groups_state = state.get("groups", {})
    policies, statuses, events = [], {}, []
    for group_name in managed_groups(proxy_data, settings.get("exclude_groups", ())):
        group = proxy_data[group_name]
        current = group.get("now")
        router_choice = (groups_state.get(group_name) or {}).get("last_router_selection")
        lock, event = update_lock(locks, group_name, current, router_choice, now)
        if lock is None:
            statuses[group_name] = {"status": "unknown_country", "current": current}
            continue
        policy = {
            "id": _policy_id(group_name, lock["country"]),
            "group_name": group_name,
            "discovery_group_name": group_name,
            "region": lock["country"],
            "region_label": regions.label(lock["country"]),
            "match": route_policy.COUNTRY_RESIDENTIAL,
            "include_pattern": "",
            "exclude_pattern": "",
            "exclude_types": ["direct"],
            "empty_fallback": "REJECT",
            "warmup_samples": 10,
            "warmup_successes": 3,
            "retire_after_seconds": 86400,
            "business_test_urls": list(urls),
            "static_candidates": [],
        }
        members = [name for name in group.get("all") or [] if is_node(proxy_data.get(name))]
        policy["static_candidates"] = [name for name in members if route_policy.name_matches(policy, name)]
        same_country = [name for name in members if regions.region_of(name)[0] == lock["country"]]
        status = {
            "status": "relocked" if lock.get("reason") == "manual_change" else "locked",
            "current": current,
            "country": lock["country"],
            "country_label": policy["region_label"],
            "locked_at": lock["locked_at"],
            "previous": lock.get("previous"),
            "previous_label": regions.label(lock["previous"]) if lock.get("previous") else None,
            "candidates": len(policy["static_candidates"]),
            "same_country_nodes": len(same_country),
            "current_is_residential": current in policy["static_candidates"],
        }
        if not policy["static_candidates"]:
            status["status"] = "no_residential"
        policy["auto_lock"] = status
        statuses[group_name] = status
        policies.append(policy)
        if event:
            events.append({"kind": event, "group": group_name, "country": lock["country"],
                           "previous": lock.get("previous"), "candidates": status["candidates"]})
    # A locked group now pointing at another group (e.g. the user picked "auto select", or a
    # config reload reset it to its first entry) is paused; the lock is kept for when it is back.
    for group_name, lock in locks.items():
        group = (proxy_data or {}).get(group_name)
        if group_name in statuses or not isinstance(group, dict):
            continue
        selected = (proxy_data or {}).get(group.get("now"))
        kind = str((selected or {}).get("type") or "").replace("-", "").lower()
        statuses[group_name] = {
            "status": "paused", "current": group.get("now"), "country": lock["country"],
            "country_label": regions.label(lock["country"]),
            # Clash's own speed-test groups pick their node themselves; we say so by name.
            "current_is_auto_group": kind in {"urltest", "fallback", "loadbalance"},
        }
    return policies, statuses, events
