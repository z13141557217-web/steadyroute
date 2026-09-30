"""AI 分流体检: where each AI domain on the reference list actually goes, and from which country.

Walks Clash's live rules (/rules) in order, reading rule-set files from Clash Verge's folder
so RULE-SET entries can be judged too, then follows the chosen group down to the node that
really carries the traffic. Read-only.
"""

import glob
import hashlib
import ipaddress
import os
import re

try:
    import ai_rules
    import regions
except ModuleNotFoundError:  # pragma: no cover
    from . import ai_rules, regions

SAMPLE_HOST = {"datadog": "browser-intake-datadoghq.com", "sift": "cdn.sift.com"}
SAMPLE_IP = {"160.79.104.0/21": "160.79.104.10", "2607:6bc0::/32": "2607:6bc0::10"}
UNKNOWN_KINDS = {"geosite", "domainregex", "processname", "processnameregex", "processpath",
                 "subrule", "domainwildcard", "and", "or", "not"}


def norm(kind):
    return str(kind).replace("-", "").replace("_", "").lower()


def domain_hit(kind, value, host):
    kind, value = norm(kind), str(value).strip().lower()
    if kind == "domain":
        return host == value
    if kind == "domainsuffix":
        return host == value or host.endswith("." + value)
    if kind == "domainkeyword":
        return value in host
    return False


def runtime_providers(text):
    """name -> {url, path, behavior, format} from the runtime config's rule-providers."""
    found, inside, current, indent = {}, False, None, None
    for line in (text or "").splitlines():
        if re.match(r"^rule-providers:\s*$", line):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            break
        if not inside or not line.strip():
            continue
        depth = len(line) - len(line.lstrip(" "))
        if indent is None:
            indent = depth
        if depth == indent and line.strip().endswith(":"):
            current = line.strip()[:-1].strip("'\"")
            found[current] = {}
        elif current and ":" in line:
            key, _, value = line.strip().partition(":")
            found[current][key.strip()] = value.strip().strip("'\"")
    return found


def load_provider(home, name, meta):
    candidates = []
    if meta.get("path"):
        candidates.append(os.path.join(home, meta["path"]))
    if meta.get("url"):
        digest = hashlib.md5(meta["url"].encode()).hexdigest()
        candidates += [os.path.join(home, "rules", digest), os.path.join(home, "ruleset", digest)]
    candidates += glob.glob(os.path.join(home, "*", name + ".*"))
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if not path:
        return None
    raw = open(path, "rb").read()
    if raw[:3] == b"MRS" or meta.get("format") == "mrs":
        return None
    behavior = (meta.get("behavior") or "").lower()
    entries = []
    for line in raw.decode("utf-8", "replace").splitlines():
        item = line.strip()
        if item.startswith("- "):
            item = item[2:].strip()
        item = item.strip("'\"")
        if not item or item.startswith("#") or item == "payload:":
            continue
        if behavior == "domain" or ("," not in item and not re.match(r"^[\d.:/a-fA-F]+$", item)):
            if item.startswith("+."):
                entries.append(("DOMAIN-SUFFIX", item[2:]))
            elif item.startswith("."):
                entries.append(("DOMAIN-SUFFIX", item[1:]))
            else:
                entries.append(("DOMAIN", item))
        elif behavior == "ipcidr" or re.match(r"^[\d.:a-fA-F]+/\d+$", item):
            entries.append(("IP-CIDR", item))
        else:
            parts = [part.strip() for part in item.split(",")]
            if len(parts) >= 2:
                entries.append((parts[0].upper(), parts[1]))
    return entries


def exit_chain(proxies, name, depth=0):
    """[group, ..., node] following each group's current selection."""
    item = proxies.get(name) or {}
    now = item.get("now")
    if not now or depth > 6 or now == name:
        return [name]
    return [name] + exit_chain(proxies, now, depth + 1)


def run(rules, proxies, runtime_text, home, line_group=None, line_country=None):
    providers = runtime_providers(runtime_text)
    cache = {}

    def provider(name):
        if name not in cache:
            cache[name] = load_provider(home, name, providers.get(name, {}))
        return cache[name]

    def first_hit(host=None, address=None):
        unsure = []
        for index, rule in enumerate(rules, 1):
            kind, payload, target = norm(rule.get("type")), str(rule.get("payload", "")), rule.get("proxy")
            if host and kind in ("domain", "domainsuffix", "domainkeyword") and domain_hit(kind, payload, host):
                return index, "%s,%s" % (rule.get("type"), payload), target, unsure
            if address is not None and kind in ("ipcidr", "ipcidr6"):
                try:
                    if address in ipaddress.ip_network(payload, strict=False):
                        return index, "%s,%s" % (rule.get("type"), payload), target, unsure
                except ValueError:
                    pass
            if kind == "ipasn" and address is not None and payload == "399358":
                return index, "IP-ASN,%s" % payload, target, unsure
            if kind == "ruleset":
                entries = provider(payload)
                if entries is None:
                    unsure.append("第 %d 条规则集 %s（无法读取内容）" % (index, payload))
                elif host and any(domain_hit(k, v, host) for k, v in entries):
                    return index, "RULE-SET,%s" % payload, target, unsure
                elif address is not None and any(
                        norm(k).startswith("ipcidr") and _contains(v, address) for k, v in entries):
                    return index, "RULE-SET,%s" % payload, target, unsure
            if kind == "match":
                return index, "MATCH", target, unsure
            if kind in UNKNOWN_KINDS:
                unsure.append("第 %d 条 %s,%s" % (index, rule.get("type"), payload))
        return None, None, None, unsure

    rows = []
    for service, entries in (("Claude", ai_rules.NETCOFFEE_CLAUDE), ("ChatGPT", ai_rules.NETCOFFEE_GPT)):
        for kind, value in entries:
            if kind == "GEOSITE":
                continue
            if kind in ("IP-CIDR", "IP-CIDR6", "IP-ASN"):
                address = ipaddress.ip_address(SAMPLE_IP.get(value, "160.79.104.10"))
                index, what, target, unsure = first_hit(address=address)
            else:
                host = SAMPLE_HOST.get(value, value if kind != "DOMAIN-SUFFIX" else "www." + value)
                index, what, target, unsure = first_hit(host=host)
            chain = exit_chain(proxies, target) if target else []
            node = chain[-1] if chain else None
            country = regions.region_of(node)[0] if node else None
            ok = bool(line_group and target == line_group and (not line_country or country == line_country))
            rows.append({
                "service": service, "rule": "%s,%s" % (kind, value), "hit": what, "index": index,
                "group": target, "chain": chain, "exit": node,
                "exit_country": regions.label(country) if country else None,
                "ok": ok, "unsure": unsure[:1],
            })
    good = sum(1 for row in rows if row["ok"])
    return {"rows": rows, "total": len(rows), "ok": good, "line_group": line_group,
            "line_country": regions.label(line_country) if line_country else None}


def _contains(network, address):
    try:
        return address in ipaddress.ip_network(network, strict=False)
    except ValueError:
        return False
