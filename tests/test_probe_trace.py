"""The detection record: compact lines, daily files, gzip of older days, bounded, exportable."""

import datetime
import gzip
import io
import json
import pathlib
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

MODULE_DIR = pathlib.Path(__file__).parents[1] / "src" / "steadyroute"
sys.path.insert(0, str(MODULE_DIR))
import probe_trace  # noqa: E402


def at(day, seconds):
    return time.mktime(datetime.datetime.strptime(day, "%Y-%m-%d").timetuple()) + seconds


class ProbeTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "trace"
        probe_trace._buffer[:] = []
        probe_trace._day = None
        probe_trace.configure(str(self.dir))

    def tearDown(self):
        probe_trace._dir = None
        self.tmp.cleanup()

    def test_lines_are_compact_and_names_are_mapped(self):
        probe_trace.record("fast", "台湾 家宽 01", 83, at("2026-10-20", 3600.25))
        probe_trace.record("fast", "台湾 家宽 01", None, at("2026-10-20", 3601))
        probe_trace.record("cycle", "台湾 家宽 02", 120, at("2026-10-20", 3602))
        probe_trace.record("confirm:transient", "台湾 家宽 01", None, at("2026-10-20", 3602.5))
        probe_trace.flush()
        lines = (self.dir / "trace-2026-10-20.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["36002\tf\t1\t83", "36010\tf\t1\t", "36020\tc\t2\t120", "36025\tk\t1\tt"])
        self.assertEqual(json.loads((self.dir / "names.json").read_text(encoding="utf-8")),
                         {"台湾 家宽 01": 1, "台湾 家宽 02": 2})

    def test_older_days_are_gzipped_and_only_three_days_kept(self):
        for day in ("2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"):
            probe_trace.record("fast", "n", 50, at(day, 60))
            probe_trace.flush()
        names = sorted(p.name for p in self.dir.iterdir() if p.name.startswith("trace-"))
        self.assertEqual(names, ["trace-2026-10-18.tsv.gz", "trace-2026-10-19.tsv.gz", "trace-2026-10-20.tsv"])
        with gzip.open(str(self.dir / "trace-2026-10-19.tsv.gz"), "rt", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "600\tf\t1\t50\n")

    def test_names_survive_a_restart(self):
        probe_trace.record("fast", "a", 1, at("2026-10-20", 1))
        probe_trace.configure(str(self.dir))
        probe_trace.record("fast", "b", 1, at("2026-10-20", 2))
        self.assertEqual(probe_trace._names, {"a": 1, "b": 2})

    def test_export_holds_days_names_events_and_meta(self):
        probe_trace.record("fast", "a", 70, at("2026-10-20", 10))
        events = pathlib.Path(self.tmp.name) / "events.jsonl"
        events.write_text(json.dumps({"ts": "2026-10-19T23:00:00+0800", "kind": "failover"}) + "\n" +
                          json.dumps({"ts": "2026-10-20T01:00:00+0800", "kind": "failover"}) + "\nbroken\n",
                          encoding="utf-8")
        archive = zipfile.ZipFile(io.BytesIO(probe_trace.export(str(events), {"version": "0.6.0"})))
        self.assertEqual(sorted(archive.namelist()), ["events.jsonl", "meta.json", "names.json", "trace-2026-10-20.tsv"])
        self.assertEqual(archive.read("events.jsonl").decode("utf-8").count("\n"), 1, "only the kept days")
        meta = json.loads(archive.read("meta.json"))
        self.assertEqual((meta["version"], meta["days"]), ("0.6.0", ["2026-10-20"]))

    def test_recording_never_raises(self):
        probe_trace._dir = str(self.dir / "gone" / "deeper")
        with mock.patch.object(probe_trace, "FLUSH_LINES", 1):
            probe_trace.record("fast", "a", 1, at("2026-10-20", 1))

    def test_a_day_of_records_stays_small(self):
        start = at("2026-10-20", 0)
        for second in range(0, 3600):                 # one hour: 2 lines a second + 20 nodes every 5 s
            probe_trace.record("fast", "AI 当前", 80, start + second)
            probe_trace.record("fast", "香港当前", 50, start + second)
            if second % 5 == 0:
                for index in range(20):
                    probe_trace.record("cycle", "节点 %d" % index, 120, start + second)
        probe_trace.flush()
        size = (self.dir / "trace-2026-10-20.tsv").stat().st_size
        self.assertLess(size * 24, 12 * 1024 * 1024, "under about 12 MB a day before gzip")


if __name__ == "__main__":
    unittest.main()
