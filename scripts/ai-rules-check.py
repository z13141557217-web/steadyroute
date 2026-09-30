"""稳航：只读检查。列出 Clash 当前生效的规则，并对照 ip.net.coffee 的 Claude / ChatGPT 规则看每个域名实际走哪个分组。不修改任何配置。"""
import glob, json, os, socket, sys, tempfile

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
            head, _, body = data.partition(b"\r\n\r\n")
            return json.loads(body.decode("utf-8"))
        except Exception as error:
            last = error
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
SITE_IP = [("IP-CIDR", "160.79.104.0/21"), ("IP-CIDR6", "2607:6bc0::/32"), ("IP-ASN", "399358")]

def norm(t):
    return t.replace("-", "").replace("_", "").lower()

rules = get("/rules").get("rules", [])
configs = get("/configs")
proxies = get("/proxies").get("proxies", {})

def first_hit(host):
    uncertain = []
    for index, rule in enumerate(rules, 1):
        kind, payload, target = norm(rule.get("type", "")), str(rule.get("payload", "")), rule.get("proxy", "")
        hit = ((kind == "domain" and host == payload.lower())
               or (kind == "domainsuffix" and (host == payload.lower() or host.endswith("." + payload.lower())))
               or (kind == "domainkeyword" and payload.lower() in host)
               or kind == "match")
        if hit:
            return index, rule, uncertain
        if kind in ("geosite", "ruleset", "domainregex", "and", "or", "not", "processname", "processpath",
                    "subrule", "domainwildcard"):
            uncertain.append("%d %s:%s→%s" % (index, rule.get("type"), payload, target))
    return None, None, uncertain

print("== 概况 ==")
print("规则总数:", len(rules), "| IPv6:", configs.get("ipv6"), "| 模式:", configs.get("mode"),
      "| TUN:", (configs.get("tun") or {}).get("enable"))
targets = {}
for rule in rules:
    targets[rule.get("proxy")] = targets.get(rule.get("proxy"), 0) + 1
print("各分组被规则指向的次数（前 15）:")
for name, count in sorted(targets.items(), key=lambda kv: -kv[1])[:15]:
    now = (proxies.get(name) or {}).get("now")
    print("  %-28s %5d 条%s" % (name, count, ("  当前节点: " + now) if now else ""))

print("\n== 指向含 AI 字样分组的规则 ==")
for index, rule in enumerate(rules, 1):
    if "AI" in str(rule.get("proxy", "")).upper():
        print("  %4d %s,%s → %s" % (index, rule.get("type"), rule.get("payload"), rule.get("proxy")))

print("\n== net.coffee 规则在你当前配置里的走向 ==")
for service, kind, value in SITE_DOMAIN:
    host = SAMPLE.get(value, value if kind != "DOMAIN-SUFFIX" else "www." + value)
    index, rule, uncertain = first_hit(host)
    if rule and norm(rule.get("type", "")) == "match":
        where = "只落到兜底 MATCH → %s" % rule.get("proxy")
    elif rule:
        where = "第 %d 条 %s,%s → %s" % (index, rule.get("type"), rule.get("payload"), rule.get("proxy"))
    else:
        where = "没有命中"
    note = ("  （但更前面的 %s 可能先命中）" % uncertain[0]) if uncertain else ""
    print("  [%s] %s,%s  →  %s%s" % (service, kind, value, where, note))

print("\n== IP / ASN 兜底 ==")
for kind, value in SITE_IP:
    found = [r for r in rules if norm(r.get("type", "")) == norm(kind) and str(r.get("payload")) == value]
    print("  %s,%s: %s" % (kind, value, ("有 → " + found[0].get("proxy")) if found else "没有"))
