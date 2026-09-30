import gzip
import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

PROJECT_DIR = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src" / "steadyroute"))
import logging_setup  # noqa: E402

DAY1 = time.mktime((2026, 9, 20, 12, 0, 0, 0, 0, -1))
DAY = 86400


class Clock(object):
    def __init__(self, value):
        self.value = float(value)

    def __call__(self):
        return self.value


def record(message, level=logging.INFO, name="steadyroute"):
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.clock = Clock(DAY1)

    def tearDown(self):
        self.tmp.cleanup()

    def handler(self, max_bytes=10000, retention_days=14, total_bytes=100000):
        handler = logging_setup.DailySizeRotatingFileHandler(
            self.dir, "router.log", max_bytes, retention_days, total_bytes, clock=self.clock)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)
        return handler

    def names(self):
        return sorted(os.listdir(self.dir))

    def test_rotates_at_day_change_and_compresses(self):
        handler = self.handler()
        handler.emit(record("day one"))
        self.clock.value += DAY
        handler.emit(record("day two"))
        self.assertIn("router-2026-09-20.log.gz", self.names())
        with gzip.open(os.path.join(self.dir, "router-2026-09-20.log.gz"), "rt") as archive:
            self.assertEqual(archive.read(), "day one\n")
        self.assertEqual(pathlib.Path(self.dir, "router.log").read_text(), "day two\n")

    def test_restart_after_midnight_rotates_yesterdays_file(self):
        handler = self.handler()
        handler.emit(record("before restart"))
        handler.close()
        os.utime(os.path.join(self.dir, "router.log"), (DAY1, DAY1))
        self.clock.value += DAY
        handler = self.handler()
        handler.emit(record("after restart"))
        self.assertIn("router-2026-09-20.log.gz", self.names())

    def test_size_limit_rolls_within_the_same_day(self):
        handler = self.handler(max_bytes=200)
        for index in range(60):
            handler.emit(record("line %03d xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" % index))
        archives = [name for name in self.names() if name.endswith(".gz")]
        self.assertGreater(len(archives), 5)
        self.assertIn("router-2026-09-20.log.gz", archives)
        self.assertIn("router-2026-09-20.1.log.gz", archives)
        self.assertLessEqual(os.path.getsize(os.path.join(self.dir, "router.log")), 200)
        for name in archives:
            with gzip.open(os.path.join(self.dir, name), "rb") as archive:
                self.assertLessEqual(len(archive.read()), 200)

    def test_single_huge_record_is_truncated(self):
        handler = self.handler(max_bytes=200)
        handler.emit(record("x" * 5000))
        self.assertLess(os.path.getsize(os.path.join(self.dir, "router.log")), 200)

    def test_retention_prunes_old_archives(self):
        for day in ("2026-09-01", "2026-09-06", "2026-09-07", "2026-09-19"):
            pathlib.Path(self.dir, "router-%s.log.gz" % day).write_bytes(b"x")
        handler = self.handler(retention_days=14)
        handler.prune()
        self.assertEqual(
            [name for name in self.names() if name.endswith(".gz")],
            ["router-2026-09-07.log.gz", "router-2026-09-19.log.gz"])

    def test_total_size_cap_drops_oldest_first(self):
        for day in ("2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"):
            pathlib.Path(self.dir, "router-%s.log.gz" % day).write_bytes(b"x" * 1000)
        handler = self.handler(total_bytes=2500)
        handler.prune()
        archives = [name for name in self.names() if name.endswith(".gz")]
        self.assertEqual(archives, ["router-2026-09-18.log.gz", "router-2026-09-19.log.gz"])

    def test_total_on_disk_never_exceeds_cap_under_load(self):
        handler = self.handler(max_bytes=2000, total_bytes=6000)
        for index in range(3000):
            handler.emit(record("cycle %d probe summary tw=%d hk=%d %s" % (index, index % 97, index % 89, os.urandom(8).hex())))
            if index % 300 == 0:
                self.clock.value += DAY
        total = sum(os.path.getsize(os.path.join(self.dir, name)) for name in self.names())
        self.assertLessEqual(total, 6000 + 2000)

    def test_compress_failure_keeps_plain_archive_and_keeps_logging(self):
        handler = self.handler()
        handler.emit(record("day one"))
        self.clock.value += DAY
        with mock.patch.object(logging_setup.gzip, "open", side_effect=OSError("disk full")):
            handler.emit(record("day two"))
        self.assertIn("router-2026-09-20.log", self.names())
        handler.emit(record("still writing"))
        self.assertIn("still writing", pathlib.Path(self.dir, "router.log").read_text())

    def test_other_streams_and_legacy_archives_are_not_mistaken_for_archives(self):
        pathlib.Path(self.dir, "router-legacy-2026-01-01.log.gz").write_bytes(b"x")
        pathlib.Path(self.dir, "router-error-2026-01-01.log.gz").write_bytes(b"x")
        handler = self.handler()
        self.assertEqual(handler.archives(), [])


class FilterTests(unittest.TestCase):
    def test_burst_filter_limits_similar_lines(self):
        clock = Clock(1000)
        burst = logging_setup.BurstFilter(limit=20, window=60, clock=clock)
        passed = [burst.filter(record("probe %d failed" % index)) for index in range(25)]
        self.assertEqual(passed.count(True), 20)
        self.assertTrue(burst.filter(record("different message")))
        clock.value += 61
        later = record("probe 99 failed")
        self.assertTrue(burst.filter(later))
        self.assertIn("省略 5 条", later.getMessage())

    def test_dedupe_filter_writes_each_error_once_per_window(self):
        clock = Clock(1000)
        dedupe = logging_setup.DedupeFilter(window=600, clock=clock)
        self.assertTrue(dedupe.filter(record("controller timeout after 3001 ms", logging.ERROR)))
        for value in range(10):
            clock.value += 30
            self.assertFalse(dedupe.filter(record("controller timeout after %d ms" % (3000 + value), logging.ERROR)))
        clock.value += 600
        later = record("controller timeout after 3005 ms", logging.ERROR)
        self.assertTrue(dedupe.filter(later))
        self.assertIn("重复 10 次", later.getMessage())

    def test_routine_limiter(self):
        limiter = logging_setup.RoutineLimiter(interval=600)
        self.assertEqual(limiter.should_emit(("keep", "tw"), "A", 0), (True, 0))
        self.assertFalse(limiter.should_emit(("keep", "tw"), "A", 20)[0])
        self.assertFalse(limiter.should_emit(("keep", "tw"), "A", 40)[0])
        self.assertEqual(limiter.should_emit(("keep", "tw"), "B", 60), (True, 2))
        self.assertFalse(limiter.should_emit(("keep", "tw"), "B", 80)[0])
        self.assertEqual(limiter.should_emit(("keep", "tw"), "B", 700), (True, 1))

    def test_limiters_have_bounded_memory(self):
        limiter = logging_setup.RoutineLimiter(max_keys=8)
        for index in range(100):
            limiter.should_emit(("key", index), "x", index)
        self.assertLessEqual(len(limiter.state), 8)
        burst = logging_setup.BurstFilter(max_keys=8)
        for index in range(100):
            burst.filter(record("unique text %s" % chr(65 + index % 26) * (index + 1)))
        self.assertLessEqual(len(burst.state), 8)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_log_is_compressed_once(self):
        pathlib.Path(self.dir, "router.log").write_text("old line\n" * 1000)
        pathlib.Path(self.dir, "router-error.log").write_text("old error\n")
        written = logging_setup.migrate_legacy_logs(self.dir, DAY1)
        self.assertEqual(len(written), 2)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "router.log")))
        with gzip.open(os.path.join(self.dir, "router-legacy-2026-09-20.log.gz"), "rt") as archive:
            self.assertEqual(archive.read(), "old line\n" * 1000)
        pathlib.Path(self.dir, "router.log").write_text("new bounded log\n")
        self.assertEqual(logging_setup.migrate_legacy_logs(self.dir, DAY1 + DAY), [])
        self.assertEqual(pathlib.Path(self.dir, "router.log").read_text(), "new bounded log\n")

    def test_legacy_archive_expires_after_90_days(self):
        pathlib.Path(self.dir, "router-legacy-2026-06-01.log.gz").write_bytes(b"x")
        pathlib.Path(self.dir, "router-legacy-2026-09-01.log.gz").write_bytes(b"x")
        logging_setup.prune_legacy(self.dir, DAY1)
        self.assertEqual(os.listdir(self.dir), ["router-legacy-2026-09-01.log.gz"])


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.hooks = (sys.excepthook, threading.excepthook, logging.raiseExceptions)

    def tearDown(self):
        for name in (logging_setup.LOGGER_NAME, logging_setup.EVENTS_LOGGER_NAME, logging_setup.NODES_LOGGER_NAME):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        sys.excepthook, threading.excepthook, logging.raiseExceptions = self.hooks
        self.tmp.cleanup()

    def read(self, name):
        return pathlib.Path(self.dir, name).read_text(encoding="utf-8")

    def test_streams_are_split_by_purpose(self):
        logger = logging_setup.configure(self.dir)
        logger.info("routine keep line")
        logger.warning("controller slow")
        logging_setup.write_event("failover", group="TW", to="TW-2")
        self.assertIn("routine keep line", self.read("router.log"))
        self.assertIn("WARNING controller slow", self.read("router.log"))
        self.assertNotIn("routine keep line", self.read("router-error.log"))
        self.assertIn("WARNING controller slow", self.read("router-error.log"))
        event = json.loads(self.read("events.jsonl").strip())
        self.assertEqual((event["kind"], event["group"], event["to"]), ("failover", "TW", "TW-2"))
        self.assertIn("unix", event)
        self.assertNotIn("failover", self.read("router.log"))
        self.assertTrue(os.path.exists(os.path.join(self.dir, logging_setup.MARKER)))

    def test_node_events_are_kept_apart_from_decisions(self):
        logging_setup.configure(self.dir)
        logging_setup.write_node_event("state_event", code="NODE_DEGRADED", scope="node")
        logging_setup.write_event("failover", group="TW")
        self.assertIn("NODE_DEGRADED", self.read("node-events.jsonl"))
        self.assertNotIn("NODE_DEGRADED", self.read("events.jsonl"))
        self.assertNotIn("failover", self.read("node-events.jsonl"))

    def test_decision_log_keeps_90_days_and_node_log_30(self):
        self.assertEqual(logging_setup.STREAMS["events"]["retention_days"], 90)
        self.assertEqual(logging_setup.STREAMS["nodes"]["retention_days"], 30)
        self.assertEqual(sum(spec["total_bytes"] for spec in logging_setup.STREAMS.values()), 38 * logging_setup.MIB)

    def test_node_archives_are_not_decision_archives(self):
        pathlib.Path(self.dir, "node-events-2026-01-01.jsonl.gz").write_bytes(b"x")
        handler = logging_setup.DailySizeRotatingFileHandler(self.dir, "events.jsonl", 1000, 90, 10000)
        self.addCleanup(handler.close)
        self.assertEqual(handler.archives(), [])

    def test_tracebacks_go_to_error_log(self):
        logger = logging_setup.configure(self.dir)
        try:
            raise ValueError("boom")
        except ValueError:
            logger.error("cycle failed", exc_info=True)
        self.assertIn("Traceback", self.read("router-error.log"))

    def test_write_event_without_configuration_is_silent(self):
        logging_setup.write_event("failover", group="TW")

    def test_stdout_mode_writes_no_files(self):
        logging_setup.configure(self.dir, to_stdout=True)
        self.assertEqual(os.listdir(self.dir), [])


if __name__ == "__main__":
    unittest.main()
