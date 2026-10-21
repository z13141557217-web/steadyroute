"""Detection record: every probe result, compact, for replaying detection rules offline.

One line per probe in LOG_DIR/trace/trace-YYYY-MM-DD.tsv (local date):

    <tenths of a second since local midnight>\t<kind>\t<node id>\t<ms or empty>[\t<real ms>]

kind: f fast lane (current node, every second), c full cycle (every candidate, 5 s),
k confirmation verdict (ms column holds t transient, c confirmed, l local offline,
h confirmed but waiting: no backup meets the targets, or a failover storm).
Since v0.6.1 fast-lane lines carry a fifth column: the real time from sending the probe to its
answer (or to its failure), which Mihomo's reported delay leaves out the connection setup of.
Node ids map to names in names.json. Days older than today are gzipped; DAYS are kept.
About 8 MB a day before compression, 2-3 MB after. Nothing leaves the Mac unless the user
downloads the record from the settings page.
"""

import datetime
import gzip
import io
import json
import os
import shutil
import threading
import time
import zipfile

DAYS = 3
FLUSH_SECONDS = 30
FLUSH_LINES = 4000
KINDS = {"fast": "f", "cycle": "c"}
VERDICTS = {"confirm:transient": "t", "confirm:confirmed": "c", "confirm:local_offline": "l",
            "confirm:hold": "h"}

_lock = threading.Lock()
_dir = None
_names = {}
_buffer = []
_day = None
_last_flush = 0.0


def configure(directory):
    """Start recording into directory (created). Without this call record() does nothing."""
    global _dir, _names
    os.makedirs(directory, exist_ok=True)
    with _lock:
        _dir = directory
        try:
            with open(os.path.join(directory, "names.json"), encoding="utf-8") as handle:
                _names = {str(k): int(v) for k, v in json.load(handle).items()}
        except (OSError, ValueError):
            _names = {}


def _node_id(name):
    if name not in _names:
        _names[name] = len(_names) + 1
        _write_names()
    return _names[name]


def _write_names():
    path = os.path.join(_dir, "names.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(_names, handle, ensure_ascii=False)
    os.replace(tmp, path)


def record(kind, node, delay, at=None, elapsed=None):
    """Never raises: the record must not be able to break detection."""
    if _dir is None or not node:
        return
    try:
        _record(kind, node, delay, at, elapsed)
    except (OSError, ValueError):
        pass


def _record(kind, node, delay, at, elapsed=None):
    at = time.time() if at is None else at
    local = datetime.datetime.fromtimestamp(at)
    day = local.strftime("%Y-%m-%d")
    tenths = int((local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6) * 10)
    if kind in VERDICTS:
        code, value = "k", VERDICTS[kind]
    else:
        code, value = KINDS.get(kind, kind[:1]), "" if delay is None else str(int(delay))
    with _lock:
        if _day is not None and day != _day:
            _flush_locked()
        _set_day(day)
        extra = "" if elapsed is None else "\t%d" % int(elapsed)
        _buffer.append("%d\t%s\t%d\t%s%s\n" % (tenths, code, _node_id(node), value, extra))
        if len(_buffer) >= FLUSH_LINES or time.monotonic() - _last_flush >= FLUSH_SECONDS:
            _flush_locked()


def _set_day(day):
    global _day
    if day != _day:
        _day = day
        _rotate(day)


def _rotate(today):
    for name in sorted(os.listdir(_dir)):
        if name.startswith("trace-") and name.endswith(".tsv") and name != "trace-%s.tsv" % today:
            path = os.path.join(_dir, name)
            with open(path, "rb") as source, gzip.open(path + ".gz", "wb") as target:
                shutil.copyfileobj(source, target)
            os.remove(path)
    kept = sorted(n for n in os.listdir(_dir) if n.startswith("trace-") and n.endswith(".tsv.gz"))
    for name in kept[:max(0, len(kept) - (DAYS - 1))]:
        os.remove(os.path.join(_dir, name))


def _flush_locked():
    global _last_flush
    _last_flush = time.monotonic()
    if not _buffer or _day is None:
        return
    with open(os.path.join(_dir, "trace-%s.tsv" % _day), "a", encoding="utf-8") as handle:
        handle.writelines(_buffer)
    del _buffer[:]


def flush():
    if _dir is None:
        return
    with _lock:
        try:
            _flush_locked()
        except OSError:
            pass


def export(events_path=None, meta=None):
    """A zip of the kept days, names.json, the matching event log lines and meta.json."""
    flush()
    out = io.BytesIO()
    with _lock, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        days = []
        if _dir is not None:
            for name in sorted(os.listdir(_dir)):
                if name.startswith("trace-") and (name.endswith(".tsv") or name.endswith(".tsv.gz")):
                    archive.write(os.path.join(_dir, name), name)
                    days.append(name[6:16])
            archive.writestr("names.json", json.dumps(_names, ensure_ascii=False, indent=1))
        if events_path and os.path.exists(events_path) and days:
            first = days[0]
            lines = []
            with open(events_path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        if str(json.loads(line).get("ts", ""))[:10] >= first:
                            lines.append(line)
                    except ValueError:
                        continue
            archive.writestr("events.jsonl", "".join(lines))
        archive.writestr("meta.json", json.dumps(dict(meta or {}, days=days, exported_at=int(time.time()),
                                                      utc_offset_seconds=time.localtime().tm_gmtoff),
                                                 ensure_ascii=False, indent=1))
    return out.getvalue()
