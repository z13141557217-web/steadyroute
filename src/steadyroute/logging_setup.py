"""Bounded, purpose-split logging for SteadyRoute (Python 3.9 standard library only).

Three files, each rotated daily and whenever it reaches its size limit, compressed
after rotation, and pruned by age and by total size:

  router.log        routine operation (rate-limited)            14 days, 5 MiB/file, 20 MiB total
  events.jsonl      decisions: failover, optimize, resume, group
                    and service state changes, one JSON/line    90 days, 1 MiB/file, 10 MiB total
  node-events.jsonl per-node lifecycle changes (noisy)         30 days, 1 MiB/file,  5 MiB total
  router-error.log  warnings, errors, tracebacks (deduplicated) 30 days, 1 MiB/file,  3 MiB total

Sizes are of the uncompressed live file; archives are gzip-compressed (typically 8-15x
smaller), so the on-disk ceiling is at most 38 MiB and in practice a few MiB.

The pre-v0.4.4 unbounded router.log is compressed once into router-legacy-<date>.log.gz.
"""

import datetime
import gzip
import json
import logging
import os
import re
import shutil
import sys
import threading
import time

LOGGER_NAME = "steadyroute"
EVENTS_LOGGER_NAME = "steadyroute.events"
NODES_LOGGER_NAME = "steadyroute.nodes"
MIB = 1024 * 1024
STREAMS = {
    "router": {"file": "router.log", "max_bytes": 5 * MIB, "retention_days": 14, "total_bytes": 20 * MIB},
    "events": {"file": "events.jsonl", "max_bytes": 1 * MIB, "retention_days": 90, "total_bytes": 10 * MIB},
    "nodes": {"file": "node-events.jsonl", "max_bytes": 1 * MIB, "retention_days": 30, "total_bytes": 5 * MIB},
    "error": {"file": "router-error.log", "max_bytes": 1 * MIB, "retention_days": 30, "total_bytes": 3 * MIB},
}
LEGACY_RETENTION_DAYS = 90
MARKER = ".steadyroute-logs-v2"
BURST_LIMIT = 20
BURST_WINDOW_SECONDS = 60
ERROR_DEDUPE_SECONDS = 600
ROUTINE_INTERVAL_SECONDS = 600
_DIGITS = re.compile(r"\d+")


def _local_day(timestamp):
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


class DailySizeRotatingFileHandler(logging.Handler):
    """Rotate at local midnight or at max_bytes; gzip archives; prune by age and total size."""

    def __init__(self, directory, filename, max_bytes, retention_days, total_bytes,
                 clock=time.time, compress=True):
        logging.Handler.__init__(self)
        self.directory = directory
        self.filename = filename
        self.path = os.path.join(directory, filename)
        self.stem, self.ext = os.path.splitext(filename)
        self.max_bytes = int(max_bytes)
        self.retention_days = int(retention_days)
        self.total_bytes = int(total_bytes)
        self.clock = clock
        self.compress = compress
        os.makedirs(directory, exist_ok=True)
        self.stream = None
        self.size = 0
        self.day = None
        self._open()

    def _open(self):
        self.stream = open(self.path, "ab")
        self.size = self.stream.tell()
        if self.size:
            self.day = _local_day(os.path.getmtime(self.path))
        else:
            self.day = _local_day(self.clock())

    def _archive_name(self, day):
        index = 0
        while True:
            suffix = "" if index == 0 else ".%d" % index
            name = "%s-%s%s%s" % (self.stem, day, suffix, self.ext)
            if not os.path.exists(os.path.join(self.directory, name)) and \
                    not os.path.exists(os.path.join(self.directory, name + ".gz")):
                return os.path.join(self.directory, name)
            index += 1

    def _archive_pattern(self):
        return re.compile(r"^%s-(\d{4}-\d{2}-\d{2})(?:\.(\d+))?%s(?:\.gz)?$" % (
            re.escape(self.stem), re.escape(self.ext)))

    def archives(self):
        """Archived files, oldest first: list of (day, index, path)."""
        pattern = self._archive_pattern()
        found = []
        for name in os.listdir(self.directory):
            match = pattern.match(name)
            if match:
                found.append((match.group(1), int(match.group(2) or 0), os.path.join(self.directory, name)))
        return sorted(found)

    def rollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                target = self._archive_name(self.day)
                os.replace(self.path, target)
                if self.compress:
                    try:
                        with open(target, "rb") as source, gzip.open(target + ".gz", "wb") as sink:
                            shutil.copyfileobj(source, sink)
                        os.unlink(target)
                    except OSError:
                        # Disk full or similar: keep the plain archive; prune still bounds the total.
                        if os.path.exists(target + ".gz"):
                            os.unlink(target + ".gz")
            self.prune()
        finally:
            self._open()
            self.day = _local_day(self.clock())

    def prune(self):
        today = datetime.date.fromtimestamp(self.clock())
        archives = self.archives()
        kept = []
        for day, index, path in archives:
            try:
                age = (today - datetime.date.fromisoformat(day)).days
            except ValueError:
                age = 0
            if age >= self.retention_days:
                os.unlink(path)
            else:
                kept.append(path)
        current = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        total = current + sum(os.path.getsize(path) for path in kept)
        while kept and total > self.total_bytes:
            oldest = kept.pop(0)
            total -= os.path.getsize(oldest)
            os.unlink(oldest)

    def emit(self, record):
        try:
            data = (self.format(record) + "\n").encode("utf-8", "replace")
            if len(data) > self.max_bytes // 2:
                data = data[: self.max_bytes // 2] + b"...[truncated]\n"
            if self.stream is None:
                self._open()
            if _local_day(self.clock()) != self.day or self.size + len(data) > self.max_bytes:
                self.rollover()
            self.stream.write(data)
            self.stream.flush()
            self.size += len(data)
        except Exception:
            self.handleError(record)

    def close(self):
        self.acquire()
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
        finally:
            self.release()
        logging.Handler.close(self)


def _message_key(record):
    return "%s|%s" % (record.name, _DIGITS.sub("#", str(record.msg))[:120])


class BurstFilter(logging.Filter):
    """Allow at most `limit` similar lines per window; note how many were dropped."""

    def __init__(self, limit=BURST_LIMIT, window=BURST_WINDOW_SECONDS, clock=time.time, max_keys=256):
        logging.Filter.__init__(self)
        self.limit, self.window, self.clock, self.max_keys = limit, window, clock, max_keys
        self.state = {}
        self.lock = threading.Lock()

    def filter(self, record):
        key, now = _message_key(record), self.clock()
        with self.lock:
            entry = self.state.get(key)
            if entry is None or now - entry["start"] >= self.window:
                dropped = entry["dropped"] if entry else 0
                if key not in self.state and len(self.state) >= self.max_keys:
                    self.state.pop(next(iter(self.state)))
                self.state[key] = {"start": now, "count": 1, "dropped": 0}
                if dropped:
                    record.msg = "%s（此前 1 分钟内同类消息省略 %d 条）" % (record.getMessage(), dropped)
                    record.args = None
                return True
            entry["count"] += 1
            if entry["count"] <= self.limit:
                return True
            entry["dropped"] += 1
            return False


class DedupeFilter(logging.Filter):
    """Write an identical warning/error at most once per window, then report the repeats."""

    def __init__(self, window=ERROR_DEDUPE_SECONDS, clock=time.time, max_keys=256):
        logging.Filter.__init__(self)
        self.window, self.clock, self.max_keys = window, clock, max_keys
        self.state = {}
        self.lock = threading.Lock()

    def filter(self, record):
        kind = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
        key = "%s|%s|%s" % (record.levelno, kind, _DIGITS.sub("#", record.getMessage())[:160])
        now = self.clock()
        with self.lock:
            entry = self.state.get(key)
            if entry is None or now - entry["at"] >= self.window:
                repeats = entry["repeats"] if entry else 0
                if key not in self.state and len(self.state) >= self.max_keys:
                    self.state.pop(next(iter(self.state)))
                self.state[key] = {"at": now, "repeats": 0}
                if repeats:
                    record.msg = "%s（过去 10 分钟内相同错误重复 %d 次）" % (record.getMessage(), repeats)
                    record.args = None
                return True
            entry["repeats"] += 1
            return False


class RoutineLimiter(object):
    """Routine lines: write when the meaning changes or every `interval` seconds."""

    def __init__(self, interval=ROUTINE_INTERVAL_SECONDS, max_keys=128):
        self.interval, self.max_keys = interval, max_keys
        self.state = {}
        self.lock = threading.Lock()

    def should_emit(self, key, meaning, now):
        with self.lock:
            entry = self.state.get(key)
            if entry is None or entry["meaning"] != meaning or now - entry["at"] >= self.interval:
                suppressed = entry["suppressed"] if entry else 0
                if key not in self.state and len(self.state) >= self.max_keys:
                    self.state.pop(next(iter(self.state)))
                self.state[key] = {"meaning": meaning, "at": now, "suppressed": 0}
                return True, suppressed
            entry["suppressed"] += 1
            return False, entry["suppressed"]


def migrate_legacy_logs(log_dir, now=None):
    """Compress the unbounded pre-v0.4.4 logs once; returns the archives written."""
    now = time.time() if now is None else now
    os.makedirs(log_dir, exist_ok=True)
    marker = os.path.join(log_dir, MARKER)
    if os.path.exists(marker):
        return []
    written = []
    for name, stem in (("router.log", "router-legacy"), ("router-error.log", "router-error-legacy")):
        path = os.path.join(log_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            target = os.path.join(log_dir, "%s-%s.log.gz" % (stem, _local_day(now)))
            with open(path, "rb") as source, gzip.open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            os.unlink(path)
            written.append(target)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("SteadyRoute bounded logs since %s\n" % _local_day(now))
    return written


def prune_legacy(log_dir, now=None):
    now = time.time() if now is None else now
    today = datetime.date.fromtimestamp(now)
    pattern = re.compile(r"^router(?:-error)?-legacy-(\d{4}-\d{2}-\d{2})\.log\.gz$")
    for name in os.listdir(log_dir):
        match = pattern.match(name)
        if match and (today - datetime.date.fromisoformat(match.group(1))).days >= LEGACY_RETENTION_DAYS:
            os.unlink(os.path.join(log_dir, name))


class _Formatter(logging.Formatter):
    def __init__(self, with_level):
        logging.Formatter.__init__(self, "[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        self.with_level = with_level

    def format(self, record):
        text = logging.Formatter.format(self, record)
        if self.with_level and record.levelno >= logging.WARNING:
            text = text.replace("] ", "] %s " % record.levelname, 1)
        return text


def configure(log_dir, to_stdout=False, clock=time.time):
    """Install the handlers. to_stdout=True is for --once/--status and interactive use."""
    logger = logging.getLogger(LOGGER_NAME)
    events = logging.getLogger(EVENTS_LOGGER_NAME)
    nodes = logging.getLogger(NODES_LOGGER_NAME)
    for item in (logger, events, nodes):
        for handler in list(item.handlers):
            item.removeHandler(handler)
            handler.close()
        item.propagate = False
    logger.setLevel(logging.INFO)
    events.setLevel(logging.INFO)
    nodes.setLevel(logging.INFO)
    if to_stdout:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_Formatter(with_level=True))
        logger.addHandler(handler)
        return logger
    # A failing log write must never turn into tracebacks on stderr (bootstrap-error.log is unbounded).
    logging.raiseExceptions = False
    migrate_legacy_logs(log_dir, clock())
    prune_legacy(log_dir, clock())
    handlers = {}
    for name, spec in STREAMS.items():
        handlers[name] = DailySizeRotatingFileHandler(
            log_dir, spec["file"], spec["max_bytes"], spec["retention_days"], spec["total_bytes"], clock=clock)
    handlers["router"].setFormatter(_Formatter(with_level=True))
    handlers["router"].addFilter(BurstFilter(clock=clock))
    handlers["error"].setLevel(logging.WARNING)
    handlers["error"].setFormatter(_Formatter(with_level=True))
    handlers["error"].addFilter(DedupeFilter(clock=clock))
    handlers["events"].setFormatter(logging.Formatter("%(message)s"))
    handlers["nodes"].setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handlers["router"])
    logger.addHandler(handlers["error"])
    events.addHandler(handlers["events"])
    nodes.addHandler(handlers["nodes"])
    install_exception_hooks(logger)
    return logger


def _write(logger_name, kind, fields):
    now = time.time()
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "unix": int(now), "kind": kind}
    for key, value in fields.items():
        # A field may not overwrite the record's own keys; keep it under a suffixed name.
        record[key + "_field" if key in ("ts", "unix", "kind") else key] = value
    logging.getLogger(logger_name).info(json.dumps(record, ensure_ascii=False, sort_keys=True))


def write_event(kind, /, **fields):
    """Append one decision record to events.jsonl (no-op when logging is not configured)."""
    _write(EVENTS_LOGGER_NAME, kind, fields)


def write_node_event(kind, /, **fields):
    """Append one node lifecycle record to node-events.jsonl (kept 30 days, apart from decisions)."""
    _write(NODES_LOGGER_NAME, kind, fields)


def install_exception_hooks(logger):
    def main_hook(kind, value, tb):
        logger.error("unhandled exception", exc_info=(kind, value, tb))

    def thread_hook(args):
        logger.error("unhandled thread exception in %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
