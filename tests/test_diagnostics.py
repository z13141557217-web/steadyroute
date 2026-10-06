"""v0.5.4 复制诊断信息: what the report says, and what it must never carry."""

import gzip
import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402,F401  (puts src/steadyroute on sys.path)

import diagnostics  # noqa: E402

NOW = 1791200000.0
HOUR = 3600


def event(kind, ago, **fields):
    fields.update({"kind": kind, "unix": int(NOW - ago),
                   "ts": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(NOW - ago))})
    return json.dumps(fields, ensure_ascii=False)


STATUS = {
    "service": {"version": "0.5.5", "started_at": NOW - 3 * HOUR, "controller_connected": True,
                "last_start": {"kind": "restart", "at": NOW - 3 * HOUR, "gap_seconds": 4},
                "memory_current_mb": 32.9, "memory_peak_mb": 33.1, "cycle_count": 540, "state_title": "运行中",
                "auto_lock_idle": {"🚀 节点选择": {"status": "excluded"}}},
    "groups": [{
        "name": "AI 台湾家宽线路", "ai_line": True, "region_label": "台湾", "current": "台湾 家宽 02", "hot_standby": "台湾 家宽 03",
        "auto_lock": {"country_label": "台湾", "candidates": 11}, "decision_code": "stable",
        "decision": "当前节点正常，专线保持出口不变", "active_connections": 25,
        "metrics": {"failovers_24h": 3, "performance_switches_24h": 0, "removal_switches_24h": 0, "adopt_switches_24h": 0},
        "connections": {"sites": [{"host": "secret-site.example", "count": 3}]},
    }],
    "nodes": {
        "台湾 家宽 01": {"region_label": "台湾", "lifecycle_title": "节点已隔离", "short_availability": 0.7, "long_availability": 0.92,
                     "latency": 88.9, "jitter": 2.1, "quality": 15, "samples": 400, "success_streak": 0, "failure_streak": 3,
                     "quarantined": True, "quarantine_until": NOW + 900},
        "台湾 家宽 02": {"region_label": "台湾", "lifecycle_title": "节点健康", "short_availability": 1.0, "long_availability": 0.97,
                     "latency": 133.2, "jitter": 3.0, "quality": 92, "samples": 400, "success_streak": 35, "failure_streak": 0},
    },
}
SETTINGS = {
    "ai_line": {"enabled": True, "country": "TW", "group_name": "AI 台湾家宽线路"},
    "managed_lines": [{"group_name": "香港家宽自动备援", "country": "HK"}],
    "applied": {"at": NOW - 2 * HOUR, "groups": ["AI 台湾家宽线路", "香港家宽自动备援"], "dropped": [["IP-ASN", "399358"]]},
    "applied_rule_count": 41, "tun": True, "ipv6": False,
    "countries": [{"code": "TW", "label": "台湾"}, {"code": "HK", "label": "香港"}],
    "line_status": {"state": "ok", "problem": None, "repaired_at": NOW - HOUR, "error": None},
    "rules_source": {"synced_at": NOW - 5 * HOUR, "checked_at": NOW - 5 * HOUR, "failures": 0, "last_error": None,
                     "urls": ["https://ip.net.coffee/claude/site.html"]},
    "clash": {"ok": True, "profile_uid": "RPxVnXa1NO4g"},
}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = pathlib.Path(self.tmp.name)
        live = [
            event("state_event", 3000, code="NODE_DISCOVERED", scope="node"),
            event("resume", 2 * HOUR + 60, gap_seconds=5400, episode_seconds=5400, brief_wakes=2),
            # 40 seconds after the wake-up: the one a reader should look at twice
            event("failover", 2 * HOUR + 20, group="AI 台湾家宽线路", **{"from": "台湾 家宽 03"}, to="台湾 家宽 01",
                  reason="confirmed_failure", failures=2, closed_connections=4, storm=True, lane="cycle", detect_seconds=21,
                  recent_probes=[[NOW - 7300, 91], [NOW - 7280, None], [NOW - 7260, None]]),
            event("failover", 1800, group="AI 台湾家宽线路", **{"from": "台湾 家宽 01"}, to="台湾 家宽 02",
                  reason="confirmed_failure", failures=2, closed_connections=1, storm=False, lane="fast", detect_seconds=3,
                  recent_probes=[[NOW - 1830, 54], [NOW - 1825, 58], [NOW - 1805, None]]),
            event("optimize", 900, group="香港家宽自动备援", **{"from": "香港 家宽 01"}, to="香港 家宽 02",
                  score_from=310.5, score_to=92.0, preserved_connections=3),
            event("service_start", 3 * HOUR, start="restart", gap_seconds=4),
            event("lines_rewritten", HOUR, status={"repairs": 1}),
            event("settings_changed", 2 * HOUR, result={"clash_change": True, "groups": ["AI 台湾家宽线路"], "rule_count": 41}),
            "not json at all",
        ]
        (self.logs / "events.jsonl").write_text("\n".join(live) + "\n", encoding="utf-8")
        day = time.strftime("%Y-%m-%d", time.localtime(NOW - 2 * 86400))
        with gzip.open(str(self.logs / ("events-%s.jsonl.gz" % day)), "wt", encoding="utf-8") as handle:
            handle.write(event("failover", 2 * 86400, group="AI 台湾家宽线路", **{"from": "台湾 家宽 05"}, to="台湾 家宽 03",
                               reason="quarantined", failures=3, closed_connections=0, storm=False, lane="cycle",
                               detect_seconds=40, recent_probes=[]) + "\n")
        old = time.strftime("%Y-%m-%d", time.localtime(NOW - 20 * 86400))
        (self.logs / ("events-%s.jsonl" % old)).write_text(
            event("failover", 20 * 86400, group="AI 台湾家宽线路", **{"from": "很久以前的节点"}, to="x", reason="confirmed_failure",
                  failures=2, closed_connections=0, storm=False, lane="cycle", detect_seconds=1, recent_probes=[]) + "\n",
            encoding="utf-8")
        (self.logs / "router-error.log").write_text(
            "[2026-10-05 09:00:00] WARNING probe of 203.0.113.77:443 failed for /Users/alex/Library/x\n"
            "[2026-10-05 09:00:05] WARNING fetch https://example.com/sub?flag=clash&id=PRIVATE123 failed\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, status=STATUS, settings=SETTINGS):
        return diagnostics.build(status, settings, "v1.19.32", str(self.logs), now=NOW,
                                 home="/Users/alex", system="macOS 15.6")

    def section(self, text, title):
        body = text.split("[%s]" % title, 1)[1] if ("[%s]" % title) in text else text.split("[" + title, 1)[1]
        return body.split("\n\n", 1)[0]

    def test_header_and_lines(self):
        text = self.report()
        head = text.split("\n\n", 1)[0]
        self.assertIn("版本 0.5.5 · 已运行 3.0 小时", head)
        self.assertIn("macOS 15.6", head)
        self.assertIn("Clash 内核 v1.19.32 · TUN 开启 · IPv6 关闭 · 控制接口已连接", head)
        lines = self.section(text, "专线")
        self.assertIn("AI 出口：台湾（分组「AI 台湾家宽线路」）· 已写入 41 条规则", lines)
        self.assertIn("其他家宽专线：香港家宽自动备援（香港）", lines)
        self.assertIn("跳过的规则：IP-ASN,399358", lines)
        self.assertIn("运行核对：与写入内容一致 · 最近一次自动重新写入", lines)
        groups = self.section(text, "线路")
        self.assertIn("AI 台湾家宽线路（AI 专线，只在故障时切换） · 锁定台湾 · 11 个家宽候选 · 当前 台湾 家宽 02 · 热备 台湾 家宽 03", groups)
        self.assertIn("近 24 小时 故障切换 3 · 回优 0", groups)
        self.assertIn("🚀 节点选择 · 未接管（excluded）", groups)

    def test_switches_say_what_the_probes_saw_and_flag_a_wake_up(self):
        text = self.report()
        records = self.section(text, "切换记录").splitlines()[1:]
        self.assertEqual(len(records), 4, "the last 7 days, the 20-day-old one left out")
        self.assertIn("回优 · 香港家宽自动备援: 香港 家宽 01 → 香港 家宽 02 · 评分 310.5 → 92.0 · 保留连接 3 个", records[0])
        self.assertIn("故障切换 · AI 台湾家宽线路: 台湾 家宽 01 → 台湾 家宽 02 · 连续失败 2 次 · 发现用时 3 秒 · 快速通道 · "
                      "断开连接 1 个 · 切换前探测 54 58 失败", records[1])
        self.assertNotIn("距唤醒或启动", records[1])
        self.assertIn("多节点同时失败 · 距唤醒或启动 40 秒", records[2])
        self.assertIn("切换前探测 91 失败 失败", records[2])
        self.assertIn("台湾 家宽 05 → 台湾 家宽 03 · 节点已隔离", records[3])
        self.assertNotIn("很久以前的节点", text)
        summary = self.section(text, "近 7 天概况")
        self.assertIn("故障切换共 3 次：唤醒或启动后 180 秒内 1 次 · 多节点同时失败 1 次 · 快速通道发现 1 次", summary)
        self.assertIn("被切走的节点：", summary)
        self.assertEqual(len([line for line in summary.splitlines() if "故障切换 " in line and "·" in line and line[2] == "-"]), 2,
                         "one line per day that has records")

    def test_service_events_and_nodes(self):
        text = self.report()
        service = self.section(text, "服务与设置")
        self.assertIn("休眠唤醒 · 休眠 1.5 小时 · 期间短暂唤醒 2 次", service)
        self.assertIn("服务启动 · 重启 · 中断 4 秒", service)
        self.assertIn("专线重新写入 · 第 1 次", service)
        self.assertIn("设置变更 · 写入 Clash：AI 台湾家宽线路，41 条规则", service)
        self.assertNotIn("NODE_DISCOVERED", text)
        nodes = self.section(text, "节点")
        self.assertIn("台湾 家宽 01 | 台湾 | 节点已隔离 | 70%/92% | 89 ms | 2 ms | 15 | 400 | 0/3 | ", nodes)
        self.assertIn("台湾 家宽 02 | 台湾 | 节点健康 | 100%/97% | 133 ms | 3 ms | 92 | 400 | 35/0 | —", nodes)
        self.assertLess(nodes.index("台湾 家宽 02"), nodes.index("台湾 家宽 01"), "healthiest first within a region")

    def test_nothing_private_leaves(self):
        text = self.report()
        for secret in ("secret-site.example", "PRIVATE123", "203.0.113.77", "/Users/alex", "alex"):
            self.assertNotIn(secret, text)
        self.assertIn("203.0.*.*:443", text)
        self.assertIn("https://example.com/sub?…", text)
        self.assertEqual(diagnostics.scrub("at 127.0.0.1:17654 and 2607:6bc0:1::10 in /Users/alex/x", "/Users/alex"),
                         "at 127.0.0.1:17654 and [IPv6] in ~/x")
        self.assertEqual(diagnostics.scrub("v1.19.32 at 23:19:50"), "v1.19.32 at 23:19:50", "versions and times are not addresses")

    def test_report_is_built_from_whatever_is_there(self):
        text = diagnostics.build(None, None, "", str(self.logs / "missing"), now=NOW, home="/Users/alex", system="macOS")
        for title in ("专线", "线路", "切换记录", "节点", "警告与错误"):
            self.assertIn("[" + title, text)
        self.assertIn("版本 未知", text)
        self.assertIn("没有接管的线路", text)
        self.assertIn("AI 出口：未启用", text)
        broken = dict(SETTINGS, controller_error="Clash 控制接口返回 HTTP 0", clash={"ok": False, "error": "没找到 Clash Verge 的配置"})
        self.assertIn("读取 Clash 失败：Clash 控制接口返回 HTTP 0", self.report(settings=broken))
        missing = dict(SETTINGS, line_status={"state": "missing", "problem": "Clash 中缺少 41 条 AI 规则（共写入 41 条）",
                                              "error": "Clash 没有接受新配置"})
        text = self.report(settings=missing)
        self.assertIn("运行核对：未生效（Clash 中缺少 41 条 AI 规则（共写入 41 条））", text)
        self.assertIn("重新写入未成功：Clash 没有接受新配置", text)

    def test_slow_exit_is_reported(self):
        with open(str(self.logs / "events.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(event("optimize", 600, group="AI 台湾家宽线路", **{"from": "台湾 家宽 02"}, to="台湾 家宽 04",
                               reason="sustained_slow", score_from=410.2, score_to=156.0, latency_from=228,
                               latency_to=123, jitter_from=121, jitter_to=22, held_cycles=30,
                               preserved_connections=5) + "\n")
        text = self.report()
        self.assertIn("换线方式：节点故障时切换；持续 10 分钟以上明显慢于备用节点时更换，24 小时内最多 3 次", text)
        self.assertIn("持续变慢 30 轮 · 延迟 228 → 123 ms · 抖动 121 → 22 ms · 评分 410.2 → 156.0 · 保留连接 5 个", text)
        off = dict(SETTINGS, ai_line=dict(SETTINGS["ai_line"], slow_exit=False))
        self.assertIn("换线方式：只在节点故障时切换（已关闭“节点变慢时更换”）", self.report(settings=off))
        # written, but the service has not made its first run-time check yet
        starting = dict(SETTINGS, line_status={"state": "off", "problem": None, "error": None})
        self.assertIn("运行核对：尚未核对（服务启动后约 20 秒开始）", self.report(settings=starting))
        never = dict(starting, applied={})
        self.assertIn("运行核对：未写入专线", self.report(settings=never))

    def test_size_is_bounded(self):
        many = [event("failover", index * 60, group="G", **{"from": "节点 %d" % index}, to="节点 %d" % (index + 1),
                      reason="confirmed_failure", failures=2, closed_connections=0, storm=False, lane="cycle",
                      detect_seconds=5, recent_probes=[[NOW, 50]] * 8) for index in range(5000)]
        (self.logs / "events.jsonl").write_text("\n".join(many) + "\n", encoding="utf-8")
        text = self.report()
        self.assertLessEqual(len(text.encode("utf-8")), diagnostics.MAX_BYTES + 64)
        self.assertEqual(len(self.section(text, "切换记录").splitlines()) - 1, diagnostics.MAX_SWITCHES)
        self.assertIn("故障切换共 5001 次", text)   # 5000 here and the archived one


if __name__ == "__main__":
    unittest.main()
