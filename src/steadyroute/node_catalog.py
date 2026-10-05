"""Read-only catalogue of every node in the Clash subscription, for the 全部节点 page.

Nothing here affects routing: SteadyRoute still only ever picks same-region residential
nodes from its own policies. The catalogue just lists what the subscription contains,
with Clash's own last delay test, so non-residential nodes are visible without being usable.
"""

import datetime
import re

try:
    import route_policy
    import regions
except ModuleNotFoundError:  # pragma: no cover - imported as a package in some tools
    from . import route_policy
    from . import regions

# Re-exported for callers and tests that read them from here.
REGIONS = regions.REGIONS
OTHER_REGION = regions.OTHER_REGION
REGION_ORDER = regions.REGION_ORDER
RESIDENTIAL_RE = regions.RESIDENTIAL_RE
INFO_RE = regions.INFO_RE


CATALOG_LIMIT = 1000

NON_NODE_TYPES = regions.NON_NODE_TYPES


def region_of(name):
    return regions.region_of(name)


def _parse_time(value):
    """RFC3339 as Mihomo writes it (nanoseconds, Z or offset) -> epoch seconds, or None."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    text = value.strip()
    zone = "+00:00"
    if text.endswith("Z"):
        text = text[:-1]
    else:
        match = re.search(r"[+-]\d{2}:\d{2}$", text)
        if match:
            zone = match.group(0)
            text = text[:match.start()]
    text = text.split(".", 1)[0]
    try:
        parsed = datetime.datetime.strptime(text + zone, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return int(parsed.timestamp())


def last_delay(proxy):
    """Clash's own most recent delay test for a node: (delay_ms or None, tested_at or None)."""
    history = proxy.get("history") if isinstance(proxy, dict) else None
    if not isinstance(history, list) or not history:
        return None, None
    entry = history[-1] if isinstance(history[-1], dict) else {}
    delay = entry.get("delay")
    tested_at = _parse_time(entry.get("time"))
    if not isinstance(delay, (int, float)) or isinstance(delay, bool):
        return None, tested_at
    return (int(delay) if delay > 0 else 0), tested_at


def _is_node(name, proxy):
    if not isinstance(name, str) or not isinstance(proxy, dict):
        return False
    if name in route_policy.BUILTIN_CANDIDATES or isinstance(proxy.get("all"), list):
        return False
    kind = str(proxy.get("type") or "").replace("-", "").lower()
    return bool(kind) and kind not in NON_NODE_TYPES


def build_catalog(proxy_data, policies, now, current=None, standby=None):
    """Every subscription node, grouped by region, with its role relative to SteadyRoute.

    role: "current" / "standby" (a SteadyRoute group uses it right now), "monitored"
    (a residential node in one of SteadyRoute's regions), or "view" (listed only; never
    selected by SteadyRoute).
    """
    current = {name for name in (current or ()) if name}
    standby = {name for name in (standby or ()) if name}
    nodes = []
    notices = 0
    for name, proxy in (proxy_data or {}).items():
        if not _is_node(name, proxy):
            continue
        if INFO_RE.search(name):
            notices += 1
            continue
        policy = next((item for item in policies if route_policy.name_matches(item, name)), None)
        code, label = region_of(name)
        if policy is not None:
            code = policy.get("region", code)
            label = dict((c, l) for c, l, _p in REGIONS).get(code, label)
        delay, tested_at = last_delay(proxy)
        if name in current:
            role = "current"
        elif name in standby:
            role = "standby"
        elif policy is not None:
            role = "monitored"
        else:
            role = "view"
        nodes.append({
            "name": name,
            "region": code,
            "region_label": label,
            "type": str(proxy.get("type") or ""),
            "udp": bool(proxy.get("udp")),
            "residential": policy is not None or bool(RESIDENTIAL_RE.search(name)),
            "group": policy["group_name"] if policy is not None else None,
            "role": role,
            "delay_ms": delay,
            "delay_at": tested_at,
        })
    role_rank = {"current": 0, "standby": 1, "monitored": 2, "view": 3}
    nodes.sort(key=lambda item: (
        REGION_ORDER.get(item["region"], len(REGIONS)),
        role_rank[item["role"]],
        item["delay_ms"] is None or item["delay_ms"] == 0,
        item["delay_ms"] or 0,
        item["name"],
    ))
    truncated = max(0, len(nodes) - CATALOG_LIMIT)
    nodes = nodes[:CATALOG_LIMIT]
    regions = []
    for item in nodes:
        if not regions or regions[-1]["code"] != item["region"]:
            regions.append({"code": item["region"], "label": item["region_label"], "count": 0, "residential": 0})
        regions[-1]["count"] += 1
        regions[-1]["residential"] += int(item["residential"])
    return {
        "generated_at": int(now),
        "total": len(nodes),
        "residential": sum(1 for item in nodes if item["residential"]),
        "monitored": sum(1 for item in nodes if item["role"] != "view"),
        "notices_hidden": notices,
        "truncated": truncated,
        "regions": regions,
        "nodes": nodes,
    }
