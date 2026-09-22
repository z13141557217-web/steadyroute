"""Bounded, process-owned logs for the SteadyRoute daemon.

launchd must not hold the log inode: its stdout/stderr are sent to /dev/null and
this module owns both rotating files. The daemon installs this before loading
runtime policy, so ordinary startup failures are captured as well.
"""

import io
import logging
from logging.handlers import RotatingFileHandler
import os
import stat
import sys
import tempfile
import threading


MAIN_MAX_BYTES = 5 * 1024 * 1024
MAIN_BACKUPS = 5
ERROR_MAX_BYTES = 1 * 1024 * 1024
ERROR_BACKUPS = 3
MAX_RECORD_CHARS = 32 * 1024

_active = None


class _BoundedFormatter(logging.Formatter):
    def __init__(self, *args, max_bytes, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_bytes = max_bytes

    def format(self, record):
        rendered = super().format(record)
        if len(rendered) > MAX_RECORD_CHARS:
            rendered = rendered[:MAX_RECORD_CHARS] + " …[truncated]"
        encoded = rendered.encode("utf-8")
        if len(encoded) + 1 > self.max_bytes:
            marker = b"...[truncated]"
            encoded = encoded[:self.max_bytes - len(marker) - 1] + marker
            return encoded.decode("utf-8", errors="ignore")
        return rendered


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


class _LineStream(io.TextIOBase):
    """Forward standard-stream writes as complete, bounded log records."""

    def __init__(self, logger, level):
        self.logger = logger
        self.level = level
        self.buffered = ""
        self.lock = threading.RLock()

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError("log stream accepts text only")
        with self.lock:
            self.buffered += value
            while "\n" in self.buffered:
                line, self.buffered = self.buffered.split("\n", 1)
                if line:
                    self.logger.log(self.level, line)
            if len(self.buffered) > MAX_RECORD_CHARS:
                self.logger.log(self.level, self.buffered[:MAX_RECORD_CHARS] + " …[truncated]")
                self.buffered = ""
        return len(value)

    def flush(self):
        with self.lock:
            if self.buffered:
                self.logger.log(self.level, self.buffered)
                self.buffered = ""


def _bound_existing(path, max_bytes):
    """Retain only the tail of an oversized pre-upgrade log before opening it."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError("unsafe log path: %s" % path)
    if metadata.st_size <= max_bytes:
        os.chmod(path, 0o600)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".steadyroute-log-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as target, open(path, "rb") as source:
            source.seek(-max_bytes, os.SEEK_END)
            target.write(source.read(max_bytes))
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _bound_log_family(path, max_bytes, backups):
    _bound_existing(path, max_bytes)
    parent = os.path.dirname(path)
    prefix = os.path.basename(path) + "."
    for name in os.listdir(parent):
        if not name.startswith(prefix) or not name[len(prefix):].isdigit():
            continue
        suffix = int(name[len(prefix):])
        archive = os.path.join(parent, name)
        if suffix <= backups:
            _bound_existing(archive, max_bytes)
        else:
            metadata = os.lstat(archive)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("unsafe log archive: %s" % archive)
            os.unlink(archive)


def _logger(name, path, max_bytes, backups):
    if max_bytes <= 64 or backups < 0:
        raise ValueError("invalid log retention limits")
    _bound_log_family(path, max_bytes, backups)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _PrivateRotatingHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(_BoundedFormatter(
        "[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S", max_bytes=max_bytes
    ))
    logger.addHandler(handler)
    return logger, handler


class _Installation:
    def __init__(self, log_dir, main_max_bytes, main_backups, error_max_bytes, error_backups):
        os.makedirs(log_dir, mode=0o700, exist_ok=True)
        self.main, self.main_handler = _logger(
            "steadyroute.main", os.path.join(log_dir, "router.log"), main_max_bytes, main_backups
        )
        try:
            self.error, self.error_handler = _logger(
                "steadyroute.error", os.path.join(log_dir, "router-error.log"), error_max_bytes, error_backups
            )
        except Exception:
            self.main.removeHandler(self.main_handler)
            self.main_handler.close()
            raise
        self.old_stdout, self.old_stderr = sys.stdout, sys.stderr
        self.old_excepthook, self.old_threading_hook = sys.excepthook, threading.excepthook
        self.stdout = _LineStream(self.main, logging.INFO)
        self.stderr = _LineStream(self.error, logging.ERROR)
        sys.stdout, sys.stderr = self.stdout, self.stderr
        sys.excepthook = self._uncaught
        threading.excepthook = self._thread_uncaught

    def _uncaught(self, kind, value, traceback):
        self.error.error("uncaught main-thread exception", exc_info=(kind, value, traceback))

    def _thread_uncaught(self, args):
        self.error.error(
            "uncaught thread exception: %s" % args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def close(self):
        global _active
        self.stdout.flush()
        self.stderr.flush()
        sys.stdout, sys.stderr = self.old_stdout, self.old_stderr
        sys.excepthook, threading.excepthook = self.old_excepthook, self.old_threading_hook
        for logger, handler in ((self.main, self.main_handler), (self.error, self.error_handler)):
            logger.removeHandler(handler)
            handler.close()
        if _active is self:
            _active = None


def install(log_dir, main_max_bytes=MAIN_MAX_BYTES, main_backups=MAIN_BACKUPS,
            error_max_bytes=ERROR_MAX_BYTES, error_backups=ERROR_BACKUPS):
    global _active
    if _active is not None:
        return _active
    _active = _Installation(log_dir, main_max_bytes, main_backups, error_max_bytes, error_backups)
    return _active


def info(message):
    if _active is None:
        stamp = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
        print("[%s] %s" % (stamp, message), flush=True)
    else:
        _active.main.info(message)


def exception(message, exc_info=None):
    if _active is None:
        import traceback
        traceback.print_exc()
    else:
        _active.error.error(message, exc_info=exc_info or sys.exc_info())
