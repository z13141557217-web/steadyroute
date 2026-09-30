"""稳航：只读检查。列出 Clash 当前生效的规则，并对照 ip.net.coffee 的 Claude / ChatGPT 规则，
看每个域名实际走哪个分组。规则集（RULE-SET）会读取本地缓存文件一起判断。不修改任何配置。"""
import glob, hashlib, ipaddress, json, os, re, socket, sys, tempfile

HOME = os.environ.get("SR_HOME") or os.path.expanduser(
    "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev")
SOCKS = ["/var/run/clash-verge-service/users/%d/verge-mihomo.sock" % os.getuid(),
         os.path.join(tempfile.gettempdir(), "verge-mihomo.sock"), "/tmp/verge/verge-mihomo.sock"]
SOCKS += glob.glob("/var/folders/*/*/T/verge-mihomo.sock")
SOCKS = [p for p in dict.fromkeys(SOCKS) if os.path.exists(p)]
if os.environ.get("SR_SOCK"):
    SOCKS = [os.environ["SR_SOCK"]]


def get(path):
    for sock_path in SOCKS:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(sock_path)
            s.sendall(("GET %s HTTP/1.0\r\nHost: localhost\r\n\r\n" % path).encode())
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            s.close()
            return json.loads(data.partition(b"\r\n\r\n")[2].decode("utf-8"))
        except Exception:
            continue
    sys.exit("连不上 Clash Verge 控制接口，请确认 Clash Verge 正在运行")


SITE_DOMAIN = [
    ("Claude", "DOMAIN-SUFFIX", "anthropic.com"), ("Claude", "DOMAIN-SUFFIX", "claude.ai"),
    ("Claude", "DOMAIN-SUFFIX", "claude.com"), ("Claude", "DOMAIN-SUFFIX", "clau.de"),
    ("Claude", "DOMAIN-SUFFIX", "claudemcpclient.com"), ("Claude", "DOMAIN-SUFFIX", "claudemcpcontent.com"),
    ("Claude", "DOMAIN-SUFFIX", "claudeusercontent.com"), ("Claude", "DOMAIN", "servd-anthropic-website.b-cdn.net"),
    ("Claude", "DOMAIN", "anthropic.com.cdn.cloudflare.net"), ("Claude", "DOMAIN", "anthropic.auth0.com"),
    ("Claude", "DOMAIN", "anthropic-com.ghost.io"), ("Claude", "DOMAIN-SUFFIX", "sentry.io"),
    ("Claude", "DOMAIN-SUFFIX", "statsigapi.net"), ("Claude", "DOMAIN", "browser-intake-us5-datadoghq.com"),
    ("Claude", "DOMAIN-KEYWORD", "datadog"), ("Claude", "DOMAIN-KEYWORD", "sift"),
    ("Claude", "DOMAIN-SUFFIX", "intercom.io"), ("Claude", "DOMAIN-SUFFIX", "intercomcdn.com"),
    ("Claude", "DOMAIN", "cdn.usefathom.com"),
    ("ChatGPT", "DOMAIN-SUFFIX", "openai.com"), ("ChatGPT", "DOMAIN-SUFFIX", "chatgpt.com"),
    ("ChatGPT", "DOMAIN-SUFFIX", "chat.com"), ("ChatGPT", "DOMAIN-SUFFIX", "sora.com"),
    ("ChatGPT", "DOMAIN-SUFFIX", "oaistatic.com"), ("ChatGPT", "DOMAIN-SUFFIX", "oaiusercontent.com"),
    ("ChatGPT", "DOMAIN-SUFFIX", "crixet.com"), ("ChatGPT", "DOMAIN-SUFFIX", "client-api.arkoselabs.com"),
    ("ChatGPT", "DOMAIN", "openai-api.arkoselabs.com"), ("ChatGPT", "DOMAIN-SUFFIX", "chatgpt.livekit.cloud"),
    ("ChatGPT", "DOMAIN-SUFFIX", "host.livekit.cloud"), ("ChatGPT", "DOMAIN-SUFFIX", "turn.livekit.cloud"),
]
SAMPLE = {"datadog": "browser-intake-datadoghq.com", "sift": "cdn.sift.com"}
SITE_IP = [("IP-CIDR", "160.79.104.0/21", "160.79.104.10"), ("IP-CIDR6", "2607:6bc0::/32", "2607:6bc0::10"),
           ("IP-ASN", "399358", None)]


def norm(t):
    return t.replace("-", "").replace("_", "").lower()


def domain_hit(kind, value, host):
    kind, value = norm(kind), value.strip().lower()
    if kind == "domain":
        return host == value
    if kind == "domainsuffix":
        return host == value or host.endswith("." + value)
    if kind == "domainkeyword":
        return value in host
    return False


# ---------------------------------------------------------------- rule providers
def runtime_providers():
    """name -> {url, path, behavior, format} from Clash Verge's generated runtime config."""
    found = {}
    for name in ("clash-verge.yaml", "clash-verge-check.yaml", "config.yaml"):
        path = os.path.join(HOME, name)
        if not os.path.exists(path):
            continue
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        inside, current, indent = False, None, None
        for line in lines:
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
        if found:
            break
    return found


def provider_rules(name, meta):
    """Return (list of (kind, value) or None, note)."""
    candidates = []
    if meta.get("path"):
        candidates.append(os.path.join(HOME, meta["path"]))
    if meta.get("url"):
        digest = hashlib.md5(meta["url"].encode()).hexdigest()
        candidates += [os.path.join(HOME, "rules", digest), os.path.join(HOME, "ruleset", digest)]
    candidates += glob.glob(os.path.join(HOME, "*", name + ".*")) + glob.glob(os.path.join(HOME, "*", name))
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if not path:
        return None, "本地缓存文件没找到"
    raw = open(path, "rb").read()
    if raw[:3] == b"MRS" or meta.get("format") == "mrs":
        return None, "二进制 .mrs 格式，无法本地判断"
    text = raw.decode("utf-8", "replace")
    behavior = (meta.get("behavior") or "").lower()
    entries = []
    for line in text.splitlines():
        item = line.strip()
        if item.startswith("- "):
            item = item[2:].strip()
        item = item.strip("'\"")
        if not item or item.startswith("#") or item == "payload:":
            continue
        if behavior == "domain" or ("," not in item and not re.match(r"^[\d.:/]+$", item)):
            if item.startswith("+."):
                entries.append(("DOMAIN-SUFFIX", item[2:]))
            elif item.startswith("."):
                entries.append(("DOMAIN-SUFFIX", item[1:]))
            else:
                entries.append(("DOMAIN", item))
        elif behavior == "ipcidr" or re.match(r"^[\d.:a-fA-F]+/\d+$", item):
            entries.append(("IP-CIDR", item))
        else:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) >= 2:
                entries.append((parts[0].upper(), parts[1]))
    return entries, "%s（%d 条，来源 %s）" % (os.path.basename(path), len(entries), meta.get("url", "本地"))


rules = get("/rules").get("rules", [])
configs = get("/configs")
proxies = get("/proxies").get("proxies", {})
providers_api = get("/providers/rules").get("providers", {})
meta = runtime_providers()
loaded = {}
for rule in rules:
    if norm(rule.get("type", "")) == "ruleset" and rule.get("payload") not in loaded:
        name = rule.get("payload")
        loaded[name] = provider_rules(name, meta.get(name, {}))


def chain(name, depth=0):
    node = (proxies.get(name) or {}).get("now")
    if not node or depth > 5:
        return name
    return name + " → " + chain(node, depth + 1)


def first_hit(host):
    skipped = []
    for index, rule in enumerate(rules, 1):
        kind, payload, target = rule.get("type", ""), str(rule.get("payload", "")), rule.get("proxy", "")
        k = norm(kind)
        if k in ("domain", "domainsuffix", "domainkeyword") and domain_hit(kind, payload, host):
            return index, "%s,%s" % (kind, payload), target, skipped
        if k == "ruleset":
            entries, note = loaded.get(payload, (None, ""))
            if entries is None:
                skipped.append("第 %d 条 RuleSet,%s（%s）" % (index, payload, note))
            elif any(domain_hit(ek, ev, host) for ek, ev in entries):
                return index, "RuleSet,%s" % payload, target, skipped
        if k == "match":
            return index, "MATCH", target, skipped
        if k in ("geosite", "domainregex", "processname", "processpath", "subrule", "domainwildcard"):
            skipped.append("第 %d 条 %s,%s" % (index, kind, payload))
    return None, None, None, skipped


print("== 概况 ==")
print("规则总数:", len(rules), "| IPv6:", configs.get("ipv6"), "| 模式:", configs.get("mode"),
      "| TUN:", (configs.get("tun") or {}).get("enable"))
print("\n== 规则集（RULE-SET）==")
for name, (entries, note) in loaded.items():
    api = providers_api.get(name) or {}
    print("  %-12s %s | 行为 %s | 条数 %s | 更新于 %s" % (
        name, note, api.get("behavior") or (meta.get(name) or {}).get("behavior"),
        api.get("ruleCount"), (api.get("updatedAt") or "")[:10]))

print("\n== 兜底 MATCH 与 AI 分组的实际出口 ==")
for rule in rules:
    if norm(rule.get("type", "")) == "match":
        print("  MATCH →", chain(rule.get("proxy")))
for name in dict.fromkeys(r.get("proxy") for r in rules if "AI" in str(r.get("proxy", "")).upper()):
    print("  ", chain(name))

print("\n== net.coffee 规则在你当前配置里的走向 ==")
tally = {}
for service, kind, value in SITE_DOMAIN:
    host = SAMPLE.get(value, value if kind != "DOMAIN-SUFFIX" else "www." + value)
    index, what, target, skipped = first_hit(host)
    tally[target] = tally.get(target, 0) + 1
    where = "第 %d 条 %s → %s" % (index, what, target) if what != "MATCH" else "兜底 MATCH → %s" % target
    note = ("  ！之前有无法判断的：%s" % skipped[0]) if skipped else ""
    print("  [%s] %s,%s  →  %s%s" % (service, kind, value, where, note))
print("  汇总：", "；".join("%s %d 个" % (k, v) for k, v in tally.items()))

print("\n== IP / ASN 兜底 ==")
for kind, value, sample in SITE_IP:
    hit = [r for r in rules if norm(r.get("type", "")) == norm(kind) and str(r.get("payload")) == value]
    via = ""
    if not hit and sample:
        address = ipaddress.ip_address(sample)
        for name, (entries, _note) in loaded.items():
            for ek, ev in entries or []:
                try:
                    if norm(ek).startswith("ipcidr") and address in ipaddress.ip_network(ev, strict=False):
                        via = "规则集 %s 里有覆盖它的 %s" % (name, ev)
                        break
                except ValueError:
                    continue
            if via:
                break
    print("  %s,%s: %s" % (kind, value, ("有 → " + hit[0].get("proxy")) if hit else (via or "没有")))
