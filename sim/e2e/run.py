"""End-to-end (v0.5.1 onwards): migrate a running v0.5.0 fixed-profile install on a fake Mac, accept the
migration on the settings page over HTTP, run the AI routing check, fail a node, uninstall.

Usage: python3 run.py WORKDIR PY39
Writes WORKDIR/results.json and screenshots under WORKDIR/shots.
"""

import http.client as httpclient
import json
import os
import pathlib
import plistlib
import shutil
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
sys.path.insert(0, str(HERE))
import fake_clash  # noqa: E402
import world  # noqa: E402

if sys.platform == "darwin":
    raise SystemExit("Linux only: this run places a stand-in Clash core under /Applications; never run it on a Mac.")
WORK = pathlib.Path(sys.argv[1]).resolve()
PY39 = sys.argv[2]
HOME = WORK / "home"
SOCKET = "/tmp/sr-e2e-mihomo.sock"
CLASH_HOME = HOME / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
OLD_APP = HOME / "Library/Application Support/Clash-Verge-Stability-Router"
CORE = pathlib.Path("/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo")
PORT = 17654
RESULT = {"steps": []}


def step(name, **data):
    entry = {"t": round(time.time() - START, 1), "step": name}
    entry.update(data)
    RESULT["steps"].append(entry)
    print(json.dumps(entry, ensure_ascii=False)[:600], flush=True)


def http(method, path, body=None, headers=None):
    connection = httpclient.HTTPConnection("127.0.0.1", PORT, timeout=60)
    base = {"Host": "127.0.0.1:%d" % PORT}
    base.update(headers or {})
    data = None if body is None else json.dumps(body).encode()
    connection.request(method, path, body=data, headers=base)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, raw.decode("utf-8", "replace")


def post(path, body):
    return http("POST", path, body, {"Origin": "http://127.0.0.1:%d" % PORT, "Content-Type": "application/json",
                                     "X-SteadyRoute": "1"})


class Launchd(object):
    """launchctl stand-in that really starts the agent's program with its environment."""

    def __init__(self):
        self.processes = {}
        self.calls = []

    def run(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["launchctl", "bootstrap"]:
            with open(argv[-1], "rb") as handle:
                data = plistlib.load(handle)
            env = dict(os.environ)
            env.update(data.get("EnvironmentVariables") or {})
            env.update({"HOME": str(HOME), "STEADYROUTE_CONTROLLER_SOCKET": SOCKET})
            env.pop("STEADYROUTE_POLICY_CONFIG", None)
            out = open(str(WORK / ("%s.out" % data["Label"])), "ab")
            args = list(data["ProgramArguments"])
            args[0] = PY39
            self.processes[argv[-1]] = subprocess.Popen(args, env=env, stdout=out, stderr=subprocess.STDOUT,
                                                        cwd=str(HOME))
            return 0, ""
        if argv[:2] == ["launchctl", "bootout"]:
            process = self.processes.pop(argv[-1], None)
            if process:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 0, ""
        return 0, ""

    def stop_all(self):
        for plist in list(self.processes):
            self.run(["launchctl", "bootout", "gui/0", plist])


def wait_status(version=None, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, data = http("GET", "/api/status")
            if status == 200 and (version is None or data["service"].get("version") == version):
                return data
        except OSError:
            pass
        time.sleep(1)
    raise SystemExit("no status")


def selections():
    return {name: item["now"] for name, item in CLASH.groups.items()}


def files():
    return {name: (CLASH_HOME / name).read_text(encoding="utf-8")
            for name in ("clash-verge.yaml", "profiles/GgroupE2E.yaml", "profiles/RrulesE2E.yaml")}


def check_summary(report):
    return {"ok": report["ok"], "total": report["total"], "line_group": report.get("line_group"),
            "line_country": report.get("line_country"),
            "misses": [[r["rule"], " → ".join(r["chain"] or []), r["exit_country"]] for r in report["rows"] if not r["ok"]]}


START = time.time()
shutil.rmtree(str(WORK), ignore_errors=True)
WORK.mkdir(parents=True)
world.build(CLASH_HOME)
ORIGINAL = files()
CORE.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(str(HERE / "fake_core.py"), str(CORE))
CORE.chmod(0o755)
CLASH = fake_clash.Clash(str(CLASH_HOME / "clash-verge.yaml"), world.LATENCY)
fake_clash.serve(CLASH, SOCKET)
cid = CLASH.open_stream("AI 台湾家宽线路", host="claude.ai", process="Claude")

# ---- 1. the old install: v0.5.0 code from its tag, fixed Taiwan / Hong Kong settings
subprocess.run(["git", "-C", str(REPO), "archive", "v0.5.0", "src/steadyroute", "-o", str(WORK / "old.tar")], check=True)
subprocess.run(["tar", "-xf", str(WORK / "old.tar"), "-C", str(WORK)], check=True)
shutil.copytree(str(WORK / "src/steadyroute"), str(OLD_APP))
(OLD_APP / "config").mkdir(exist_ok=True)
shutil.copy2(str(REPO / "tests/fixtures/route-policies.fixed.json"), str(OLD_APP / "config/route-policies.json"))
(OLD_APP / "VERSION").write_text("0.5.0\n", encoding="utf-8")
agents = HOME / "Library/LaunchAgents"
agents.mkdir(parents=True)
old_plist = agents / "com.example.clash-stability-router.plist"
with open(str(old_plist), "wb") as handle:
    plistlib.dump({"Label": "com.example.clash-stability-router",
                   "ProgramArguments": ["/usr/bin/python3", str(OLD_APP / "weighted_router.py"), "--daemon"],
                   "RunAtLoad": True, "KeepAlive": True}, handle)
LAUNCHD = Launchd()
LAUNCHD.run(["launchctl", "bootstrap", "gui/0", str(old_plist)])
old = wait_status("0.5.0")
time.sleep(45)
old = wait_status("0.5.0")
step("old install running", version=old["service"]["version"], profile=old["service"].get("profile"),
     groups=[g["name"] for g in old["groups"]], state=(OLD_APP / "state.json").exists())

# ---- 2. install.command from the repository
sys.path.insert(0, str(REPO / "scripts"))
import installer  # noqa: E402
printed = []
INSTALL_OUTPUT = printed
installer.say = lambda message="": printed.append(message)
inst = installer.Installer(REPO, home=HOME, uid=501, system="Darwin", run=LAUNCHD.run, python=PY39,
                           sockets=[SOCKET])
t0 = time.time()
snapshot = inst.install(open_dashboard=False)
import threading  # noqa: E402
REC = {"status": [], "markers": [], "api": {}}
REC_STOP = threading.Event()


REC_EVERY = [2]


def recorder():
    while not REC_STOP.is_set():
        try:
            status, data = http("GET", "/api/status")
            if status == 200:
                REC["status"].append({"at": time.time(), "data": data})
        except Exception:
            pass
        REC_STOP.wait(REC_EVERY[0])


def mark(label):
    REC["markers"].append({"at": time.time(), "label": label})


mark("安装完成：旧版迁移，4 个分组已接管")
threading.Thread(target=recorder, daemon=True).start()
step("installed", seconds=round(time.time() - t0, 1), output=list(printed),
     version=snapshot["service"]["version"], old_plist_left=old_plist.exists(),
     state_carried=(HOME / "Library/Application Support/SteadyRoute/state.json").exists(),
     logs=sorted(p.name for p in (HOME / "Library/Logs/SteadyRoute").iterdir()))
assert files() == ORIGINAL, "installing must not touch Clash"
time.sleep(25)
status = wait_status(VERSION)
step("after install", groups=[(g["name"], (g.get("auto_lock") or {}).get("country_label"), g["current"])
                              for g in status["groups"]], idle=status["service"].get("auto_lock_idle"))
RESULT["status_after_install"] = status

code, settings = http("GET", "/api/settings")
REC["api"]["settings_before"] = settings
step("settings", migration=settings.get("migration"), countries=settings.get("countries"), clash=settings.get("clash"))
code, before_check = http("GET", "/api/ai-check")
REC["api"]["check_before"] = before_check
RESULT["check_before"] = before_check
step("ai check before", **check_summary(before_check))
rules_before = list(CLASH.rules)

# ---- 2b. what the page shows before migrating
try:
    from playwright.sync_api import sync_playwright
    shots = WORK / "shots"
    shots.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto("http://127.0.0.1:%d/settings" % PORT)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(shots / "before-settings.png"), full_page=False)
        page.evaluate("document.querySelector('#check').open=true")
        page.wait_for_timeout(1500)
        page.locator("#check").screenshot(path=str(shots / "before-check.png"))
        page.evaluate("window.scrollTo(0, 0)")
        page.click("#migration-accept")
        page.wait_for_selector("dialog[open]")
        page.wait_for_timeout(500)
        page.screenshot(path=str(shots / "before-dialog.png"), full_page=False)
        page.click("#dlg-cancel")
        browser.close()
except Exception as error:
    step("early screenshots failed", error=str(error))
assert files() == ORIGINAL, "cancelling the dialog must not touch Clash"

time.sleep(15)
# ---- 3. page guard
bad, _ = http("POST", "/api/settings/apply", {"migration": "accept"},
              {"Origin": "http://evil.example", "Content-Type": "application/json", "X-SteadyRoute": "1"})
step("foreign origin refused", status=bad)

try:   # what v0.5.1 did, and what a real Clash Verge in service mode answered
    CLASH.put_config({"path": str(CLASH_HOME / "clash-verge.yaml")})
    refusal = None
except ValueError as error:
    refusal = str(error)
step("core refuses a file outside its home", refused=bool(refusal), message=(refusal or "")[:160], core_home=CLASH.home)
CLASH.refused = 0
# ---- 4. preview and accept the migration
code, preview = post("/api/settings/preview", {"migration": "accept"})
RESULT["preview"] = preview
assert files() == ORIGINAL, "preview must not touch Clash"
step("preview", code=code, lines=[(l["group"], l["country"], l["added"], l["removed"], len(l["after"])) for l in preview["lines"]],
     removed_legacy=preview.get("removed_legacy"), rule_count=preview.get("rule_count"), breakdown=preview.get("rule_breakdown"))
sel_before = selections()
t0 = time.time()
REC["api"]["preview_migration"] = preview
mark("设置页确认迁移：AI 专线与规则写入 Clash")
code, applied = post("/api/settings/apply", {"migration": "accept"})
REC["api"]["apply_migration"] = applied
REC["api"]["settings_after"] = http("GET", "/api/settings")[1]
for key, change in (("ai_jp", {"ai_line": {"enabled": True, "country": "JP"}}),
                    ("ai_off", {"ai_line": {"enabled": False}}),
                    ("manual_gemini", {"manual": ["gemini.google.com"]}),
                    ("exclude_only", {"exclude_groups": ["家宽出口"]})):
    REC["api"]["preview_" + key] = post("/api/settings/preview", change)[1]
REC["api"]["dismiss_settings"] = None
step("apply", code=code, result=applied, seconds=round(time.time() - t0, 2), reloads=CLASH.reloads, refused_paths=CLASH.refused)
after_files = files()
RESULT["files_after"] = after_files
sel_after = selections()
step("selections kept", before={k: sel_before.get(k) for k in ("AI 台湾家宽线路", "香港家宽自动备援", "🚀 节点选择", "家宽出口")},
     after={k: sel_after.get(k) for k in ("AI 台湾家宽线路", "香港家宽自动备援", "🚀 节点选择", "家宽出口")})
step("groups in clash", ai=json.loads(json.dumps(CLASH.groups.get("AI 台湾家宽线路"))), hk=json.loads(json.dumps(CLASH.groups.get("香港家宽自动备援"))),
     discovery_left=[n for n in CLASH.groups if n.startswith("SteadyRoute 发现")])
ours = len(applied.get("groups") and json.load(open(str(HOME / "Library/Application Support/SteadyRoute/clash-applied.json")))["rules"])
step("user rules untouched", ours=ours, same=CLASH.rules[ours:] == rules_before,
     first=CLASH.rules[:3], last=CLASH.rules[-2:])

code, after_check = http("GET", "/api/ai-check")
REC["api"]["check_after"] = after_check
RESULT["check_after"] = after_check
step("ai check after", **check_summary(after_check))

time.sleep(25)
status = wait_status(VERSION)
RESULT["status_after_apply"] = status
step("dashboard", groups=[(g["name"], g.get("ai_line"), (g.get("auto_lock") or {}).get("country_label"), g["current"],
                           g.get("hot_standby"), g.get("decision")) for g in status["groups"]])

# ---- 4b. "节点变慢时更换" is a SteadyRoute setting: switching it never writes to Clash
reloads_before, files_before = CLASH.reloads, files()
toggles = []
for value in (False, True):
    code, plan = post("/api/settings/preview", {"ai_line": {"slow_exit": value}})
    code, done = post("/api/settings/apply", {"ai_line": {"slow_exit": value}})
    toggles.append((value, code, plan, done, http("GET", "/api/settings")[1]["ai_line"]))
step("slow exit switch", toggles=toggles, reloads=CLASH.reloads - reloads_before, files_same=files() == files_before)
assert CLASH.reloads == reloads_before and files() == files_before

# ---- 4c. the AI line's node keeps answering but turns slow and jittery. It never fails, so a
#          failover never comes: the slow-node exit moves the line after about ten minutes.
AI = "AI 台湾家宽线路"
slow = CLASH.groups[AI]["now"]
normal = (CLASH.latency[slow], CLASH.jitter[slow])
chats = [CLASH.open_stream(AI, "claude.ai", "Claude"), CLASH.open_stream(AI, "api.anthropic.com", "claude")]
mark("当前节点变慢：%s 仍然连通，延迟和抖动升高" % slow)
CLASH.latency[slow], CLASH.jitter[slow] = 300, 150
REC_EVERY[0] = 4
t0 = time.time()
seen, selects_before = {}, len([e for e in CLASH.log if e["kind"] == "select"])
closes_before = len([e for e in CLASH.log if e["kind"] == "close"])
while time.time() - t0 < 20 * 60 and CLASH.groups[AI]["now"] == slow:
    group = [g for g in http("GET", "/api/status")[1]["groups"] if g.get("ai_line")][0]
    if "正在确认" in group["decision"] and "confirming" not in seen:
        seen["confirming"] = {"at": round(time.time() - t0), "title": group["decision"], "detail": group["decision_detail"],
                              "code": group["decision_code"], "failovers_24h": group.get("failovers_24h")}
        mark("看板显示：%s" % group["decision"])
    if group["decision_code"] in ("failover_now",):
        seen["failed"] = True
    time.sleep(2)
moved = CLASH.groups[AI]["now"]
waited = round(time.time() - t0, 1)
mark("因持续变慢更换：%s → %s（旧连接保留）" % (slow, moved))
time.sleep(8)
group = [g for g in http("GET", "/api/status")[1]["groups"] if g.get("ai_line")][0]
events = [json.loads(line) for line in (HOME / "Library/Logs/SteadyRoute/events.jsonl").read_text(encoding="utf-8").splitlines()
          if '"optimize"' in line]
step("slow exit", slow=slow, now=moved, seconds=waited, same_country=moved in world.TW, never_failed="failed" not in seen,
     confirming=seen.get("confirming"), after_title=group["decision"], after_detail=group["decision_detail"],
     closed_connections=len([e for e in CLASH.log if e["kind"] == "close"]) - closes_before,
     old_connections_still_on_old_node=[CLASH.connections.get(cid, {}).get("node") == slow for cid in chats],
     selects=len([e for e in CLASH.log if e["kind"] == "select"]) - selects_before,
     event=events[-1] if events else None)
assert moved != slow and moved in world.TW, "the slow node was not left for a Taiwan residential node"
assert all(CLASH.connections.get(cid, {}).get("node") == slow for cid in chats), "an existing connection was cut"
CLASH.latency[slow], CLASH.jitter[slow] = normal
for cid in chats:
    CLASH.connections.pop(cid, None)
REC_EVERY[0] = 2
time.sleep(20)

# ---- 5. a Taiwan node fails: the replacement must be Taiwan residential
current = CLASH.groups["AI 台湾家宽线路"]["now"]
mark("台湾节点断开：%s" % current)
CLASH.down_nodes.add(current)
t0 = time.time()
while time.time() - t0 < 40 and CLASH.groups["AI 台湾家宽线路"]["now"] == current:
    time.sleep(0.5)
new = CLASH.groups["AI 台湾家宽线路"]["now"]
step("failover", failed=current, now=new, seconds=round(time.time() - t0, 1), same_country=new in world.TW)
mark("故障切换完成：%s" % new)
time.sleep(40)
CLASH.down_nodes.discard(current)
selects = [e for e in CLASH.log if e["kind"] == "select"]
cross = [e for e in selects if (e["group"] in ("AI 台湾家宽线路",) and e["node"] not in world.TW)
         or (e["group"] == "香港家宽自动备援" and e["node"] not in world.HK)]
step("cross-region selections", count=len(cross), selects=len(selects))

# ---- 5-. after the failover the AI line is described as what it is: no "回优", no performance cooldown
ai_group = [g for g in http("GET", "/api/status")[1]["groups"] if g.get("ai_line")][0]
step("ai line wording after failover", code=ai_group["decision_code"], title=ai_group["decision"],
     detail=ai_group["decision_detail"], next_action=ai_group["next_action"],
     no_optimise_words=not any(word in ai_group[key] for word in ("回优", "最佳", "冷却")
                               for key in ("decision", "decision_detail", "next_action")))
assert ai_group["decision_code"] != "cooldown", "the AI line has no performance cooldown"

# ---- 5a. Clash Verge reloads the config it keeps in memory (the one from before our change):
#          the files it rebuilds from still hold our block, the core no longer runs it
written_runtime = files()["clash-verge.yaml"]
reloads_before = CLASH.reloads
mark("Clash Verge 重新加载了旧配置：AI 专线从 Clash 中消失")
(CLASH_HOME / "clash-verge.yaml").write_text(ORIGINAL["clash-verge.yaml"], encoding="utf-8")
CLASH.load_text(ORIGINAL["clash-verge.yaml"], "clash verge (old config)")
t0 = time.time()
during_check = http("GET", "/api/ai-check")[1]
noticed = repaired = None
shown = {}
while time.time() - t0 < 90 and repaired is None:
    watch = http("GET", "/api/settings")[1]["line_status"]
    if watch["state"] == "missing" and noticed is None:
        noticed = round(time.time() - t0, 1)
        shown = {"problem": watch["problem"],
                 "dashboard": [(g["name"], g.get("line_state")) for g in http("GET", "/api/status")[1]["groups"] if g.get("ai_line")]}
        REC["api"]["settings_missing"] = http("GET", "/api/settings")[1]
    if watch["state"] == "ok" and watch["repairs"]:
        repaired = round(time.time() - t0, 1)
    time.sleep(0.5)
healed_check = http("GET", "/api/ai-check")[1]
step("lines rewritten after clash verge reloaded its old config", noticed_seconds=noticed, repaired_seconds=repaired,
     shown=shown, check_while_missing=[during_check["ok"], during_check["total"]],
     check_after=[healed_check["ok"], healed_check["total"]], reloads_by_steadyroute=CLASH.reloads - reloads_before - 1,
     runtime_same_as_written=files()["clash-verge.yaml"] == written_runtime,
     node_kept=CLASH.groups["AI 台湾家宽线路"]["now"] == new)
mark("稳航发现后重新写入：AI 专线恢复")
assert repaired is not None, "the lines were not written again"
time.sleep(25)

REC["api"]["diagnostics"] = http("GET", "/api/diagnostics")[1]
step("diagnostics", bytes=REC["api"]["diagnostics"]["bytes"], lines=REC["api"]["diagnostics"]["text"].count("\n"),
     has_failover="故障切换 · AI 台湾家宽线路" in REC["api"]["diagnostics"]["text"],
     has_rewrite="专线重新写入" in REC["api"]["diagnostics"]["text"],
     leaks_home=str(HOME) in REC["api"]["diagnostics"]["text"])

# ---- 5b. AI line off (the old group comes back, user rules stay valid) and on again
mark("关闭 AI 专线：恢复原来的分组定义")
code, off = post("/api/settings/apply", {"ai_line": {"enabled": False}})
REC["api"]["apply_ai_off"] = off
REC["api"]["settings_off"] = http("GET", "/api/settings")[1]
REC["api"]["check_off"] = http("GET", "/api/ai-check")[1]
old_def = CLASH.groups.get("AI 台湾家宽线路")
step("ai line off", code=code, result=off, group=json.loads(json.dumps(old_def)), check_ok=REC["api"]["check_off"].get("ok"),
     user_rules_same=CLASH.rules == rules_before)
time.sleep(12)
REC["api"]["preview_ai_tw_again"] = post("/api/settings/preview", {"ai_line": {"enabled": True, "country": "TW"}})[1]
mark("重新开启 AI 专线（台湾）")
code, on = post("/api/settings/apply", {"ai_line": {"enabled": True, "country": "TW"}})
step("ai line on again", code=code, result=on, check=http("GET", "/api/ai-check")[1]["ok"])
time.sleep(12)

# ---- 6. screenshots
RESULT["shots"] = []
try:
    from playwright.sync_api import sync_playwright
    shots = WORK / "shots"
    shots.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None)
        for scheme in ("light", "dark"):
            page = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
            page.goto("http://127.0.0.1:%d/" % PORT)
            page.wait_for_timeout(2500)
            page.screenshot(path=str(shots / ("dashboard-%s.png" % scheme)), full_page=False)
            page.goto("http://127.0.0.1:%d/settings" % PORT)
            page.wait_for_timeout(1500)
            page.evaluate("document.querySelector('#check').open=true")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shots / ("settings-%s.png" % scheme)), full_page=True)
            page.locator("#check").screenshot(path=str(shots / ("after-check-%s.png" % scheme)))
            page.close()
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        page.goto("http://127.0.0.1:%d/settings" % PORT)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(shots / "settings-mobile.png"), full_page=True)
        browser.close()
    RESULT["shots"] = sorted(p.name for p in shots.iterdir())
except Exception as error:
    step("screenshots failed", error=str(error))

REC_STOP.set()
json.dump(REC, open(str(WORK / "recording.json"), "w"), ensure_ascii=False)
# ---- 7. uninstall: Clash back to what it was
inst2 = installer.Installer(REPO, home=HOME, uid=501, system="Darwin", run=LAUNCHD.run, python=PY39, sockets=[SOCKET])
printed.clear()
inst2.uninstall()
restored = files()
step("uninstall", output=list(printed), groups_ext_restored=restored["profiles/GgroupE2E.yaml"] == ORIGINAL["profiles/GgroupE2E.yaml"],
     rules_ext_restored=restored["profiles/RrulesE2E.yaml"] == ORIGINAL["profiles/RrulesE2E.yaml"],
     runtime_same_groups=sorted(CLASH.groups) == sorted(fake_clash.Clash(str(WORK / "orig.yaml") if False else str(CLASH_HOME / "clash-verge.yaml"), {}).groups),
     rules_restored=CLASH.rules == rules_before, service_left=bool(LAUNCHD.processes))
import yaml  # noqa: E402
step("runtime restored (parsed)", equal=yaml.safe_load(restored["clash-verge.yaml"]) == yaml.safe_load(ORIGINAL["clash-verge.yaml"]))
LAUNCHD.stop_all()
RESULT["clash_log"] = [e for e in CLASH.log if e["kind"] in ("reload", "select", "geo")]
json.dump(RESULT, open(str(WORK / "results.json"), "w"), ensure_ascii=False, indent=1)
print("done")
