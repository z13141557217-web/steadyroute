"""AI line routing rules. Standard library only.

Priority, highest first (all rules point at the AI line group and are placed before every
rule of the user's own):

1. ip.net.coffee "Claude 分流规则大全" and "ChatGPT / Codex 分流规则大全" — the reference the
   project follows. A snapshot ships with the program; a weekly sync keeps it current.
2. The community aggregate GEOSITE,category-ai-!cn (v2fly domain-list-community), which Mihomo
   carries in its own geosite database (other AI services: Gemini, Grok, Perplexity ...).
3. AI desktop apps and CLIs by process name.
4. Domains the user added on the settings page.

Not taken from net.coffee: GEOSITE,category-ntp. NTP needs UDP and the AI line disables UDP
(so QUIC cannot bypass the residential exit); routing NTP there would break time sync.
"""

import html
import ipaddress
import json
import re

NETCOFFEE_CLAUDE_URL = "https://ip.net.coffee/claude/site.html"
NETCOFFEE_GPT_URL = "https://ip.net.coffee/gpt/site.html"
SNAPSHOT_DATE = "2026-09-30"

# Verbatim from the two pages (target group stripped). ("TYPE", "value")
NETCOFFEE_CLAUDE = (
    ("DOMAIN-SUFFIX", "anthropic.com"), ("DOMAIN-SUFFIX", "claude.ai"),
    ("DOMAIN-SUFFIX", "claude.com"), ("DOMAIN-SUFFIX", "clau.de"),
    ("DOMAIN-SUFFIX", "claudemcpclient.com"), ("DOMAIN-SUFFIX", "claudemcpcontent.com"),
    ("DOMAIN-SUFFIX", "claudeusercontent.com"),
    ("DOMAIN", "servd-anthropic-website.b-cdn.net"), ("DOMAIN", "anthropic.com.cdn.cloudflare.net"),
    ("DOMAIN", "anthropic.auth0.com"), ("DOMAIN", "anthropic-com.ghost.io"),
    ("DOMAIN-SUFFIX", "sentry.io"), ("DOMAIN-SUFFIX", "statsigapi.net"),
    ("DOMAIN", "browser-intake-us5-datadoghq.com"),
    ("DOMAIN-KEYWORD", "datadog"), ("DOMAIN-KEYWORD", "sift"),
    ("DOMAIN-SUFFIX", "intercom.io"), ("DOMAIN-SUFFIX", "intercomcdn.com"),
    ("DOMAIN", "cdn.usefathom.com"),
    ("IP-CIDR", "160.79.104.0/21"), ("IP-CIDR6", "2607:6bc0::/32"), ("IP-ASN", "399358"),
)
NETCOFFEE_GPT = (
    ("GEOSITE", "openai"),
    ("DOMAIN-SUFFIX", "openai.com"), ("DOMAIN-SUFFIX", "chatgpt.com"), ("DOMAIN-SUFFIX", "chat.com"),
    ("DOMAIN-SUFFIX", "sora.com"), ("DOMAIN-SUFFIX", "oaistatic.com"),
    ("DOMAIN-SUFFIX", "oaiusercontent.com"), ("DOMAIN-SUFFIX", "crixet.com"),
    ("DOMAIN-SUFFIX", "client-api.arkoselabs.com"), ("DOMAIN", "openai-api.arkoselabs.com"),
    ("DOMAIN-SUFFIX", "chatgpt.livekit.cloud"), ("DOMAIN-SUFFIX", "host.livekit.cloud"),
    ("DOMAIN-SUFFIX", "turn.livekit.cloud"),
)
COMMUNITY = (("GEOSITE", "category-ai-!cn"),)
# Desktop apps and CLIs whose every connection is AI traffic. Cursor-like editors are left
# to the domain lists: they also download extensions and packages.
AI_PROCESSES = ("Claude", "Claude Helper", "claude", "ChatGPT", "codex")
# Rules a Clash core may lack the database for; dropped (and reported) if validation fails.
OPTIONAL = (("GEOSITE", "category-ai-!cn"), ("IP-ASN", "399358"))

ALLOWED_TYPES = {"DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "IP-CIDR", "IP-CIDR6", "IP-ASN", "GEOSITE"}
NEVER_GEOSITE = {"cn", "geolocation-cn", "geolocation-!cn", "category-ntp", "private", "gfw", "tld-!cn"}
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$")
ANCHORS = {
    "claude": {("DOMAIN-SUFFIX", "anthropic.com"), ("DOMAIN-SUFFIX", "claude.ai")},
    "gpt": {("DOMAIN-SUFFIX", "openai.com"), ("DOMAIN-SUFFIX", "chatgpt.com")},
}
LINE_RE = re.compile(
    r"(DOMAIN-SUFFIX|DOMAIN-KEYWORD|DOMAIN|IP-CIDR6|IP-CIDR|IP-ASN|GEOSITE)\s*,\s*([^\s,<>'\"`]+)", re.I)


class RuleSourceError(ValueError):
    """A fetched rule list failed the safety checks; the previous list stays in use."""


def valid_entry(kind, value):
    kind, value = kind.upper(), value.strip()
    if kind not in ALLOWED_TYPES:
        return False
    if kind in ("DOMAIN", "DOMAIN-SUFFIX"):
        return bool(DOMAIN_RE.match(value.lower())) and value.count(".") >= 1
    if kind == "DOMAIN-KEYWORD":
        return bool(re.fullmatch(r"[a-z0-9-]{4,40}", value.lower()))
    if kind in ("IP-CIDR", "IP-CIDR6"):
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
        minimum = 12 if network.version == 4 else 24
        return network.prefixlen >= minimum and (network.version == 6) == (kind == "IP-CIDR6")
    if kind == "IP-ASN":
        return value.isdigit() and 0 < int(value) < 4294967296
    if kind == "GEOSITE":
        return bool(re.fullmatch(r"[a-z0-9@!_-]{2,40}", value.lower())) and value.lower() not in NEVER_GEOSITE
    return False


def parse_page(text):
    """Rule entries found in a net.coffee rules page (HTML or text), in page order, deduplicated."""
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    found = []
    for kind, value in LINE_RE.findall(text):
        kind = kind.upper()
        if kind == "GEOSITE":
            value = value.lower()
        entry = (kind, value)
        if entry not in found and valid_entry(kind, value):
            found.append(entry)
    return found


def check_source(name, entries, previous=None):
    """Safety checks before a synced list replaces the one in use. Raises RuleSourceError."""
    entries = [tuple(item) for item in entries]
    if not 3 <= len(entries) <= 200:
        raise RuleSourceError("%s: %d rules is outside the expected range" % (name, len(entries)))
    missing = ANCHORS[name] - set(entries)
    if missing:
        raise RuleSourceError("%s: core domains missing (%s); page layout probably changed" % (
            name, ", ".join(value for _kind, value in sorted(missing))))
    bad = [entry for entry in entries if not valid_entry(*entry)]
    if bad:
        raise RuleSourceError("%s: unsafe rule %s,%s" % ((name,) + bad[0]))
    if previous:
        removed = set(map(tuple, previous)) - set(entries)
        if len(removed) > max(3, len(previous) // 2):
            raise RuleSourceError("%s: %d of %d rules would disappear at once" % (
                name, len(removed), len(previous)))
    return entries


def diff(previous, current):
    before, after = set(map(tuple, previous or ())), set(map(tuple, current))
    return {"added": sorted(after - before), "removed": sorted(before - after)}


def manual_entry(domain):
    """A domain typed on the settings page, as a DOMAIN-SUFFIX entry, or None if invalid."""
    value = domain.strip().lower().rstrip(".")
    value = re.sub(r"^(?:https?://)?(?:\*\.|\+\.|\.)?", "", value).split("/")[0]
    return ("DOMAIN-SUFFIX", value) if valid_entry("DOMAIN-SUFFIX", value) else None


def render(kind, value, group):
    line = "%s,%s,%s" % (kind, value, group)
    if kind in ("IP-CIDR", "IP-CIDR6", "IP-ASN"):
        line += ",no-resolve"
    return line


def build(group, claude=None, gpt=None, manual=(), drop=(), processes=AI_PROCESSES):
    """Ordered rule lines for the AI line, plus a breakdown for the settings page."""
    sections = [
        ("net.coffee · Claude", list(claude or NETCOFFEE_CLAUDE)),
        ("net.coffee · ChatGPT / Codex", list(gpt or NETCOFFEE_GPT)),
        ("社区合集 category-ai-!cn", list(COMMUNITY)),
        ("AI 桌面 App", [("PROCESS-NAME", name) for name in processes]),
        ("手动添加", [tuple(item) for item in manual]),
    ]
    dropped = {tuple(item) for item in drop}
    lines, seen, breakdown = [], set(), []
    for title, entries in sections:
        count = 0
        for kind, value in entries:
            if (kind, value) in dropped:
                continue
            line = render(kind, value, group)
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
            count += 1
        breakdown.append({"source": title, "count": count})
    return lines, breakdown


def dumps_state(claude, gpt, fetched_at, note=""):
    return json.dumps({"claude": [list(item) for item in claude], "gpt": [list(item) for item in gpt],
                       "fetched_at": int(fetched_at), "note": note}, ensure_ascii=False)
