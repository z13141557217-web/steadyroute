"""Process memory metrics without third-party packages."""

import ctypes
import ctypes.util
import resource
import sys

_TASK_VM_INFO = 22


class _TaskVMInfo(ctypes.Structure):
    # <mach/task_info.h> task_vm_info, up to and including phys_footprint (REV1).
    _pack_ = 4
    _fields_ = [
        ("virtual_size", ctypes.c_uint64), ("region_count", ctypes.c_int32),
        ("page_size", ctypes.c_int32), ("resident_size", ctypes.c_uint64),
        ("resident_size_peak", ctypes.c_uint64), ("device", ctypes.c_uint64),
        ("device_peak", ctypes.c_uint64), ("internal", ctypes.c_uint64),
        ("internal_peak", ctypes.c_uint64), ("external", ctypes.c_uint64),
        ("external_peak", ctypes.c_uint64), ("reusable", ctypes.c_uint64),
        ("reusable_peak", ctypes.c_uint64), ("purgeable_volatile_pmap", ctypes.c_uint64),
        ("purgeable_volatile_resident", ctypes.c_uint64),
        ("purgeable_volatile_virtual", ctypes.c_uint64), ("compressed", ctypes.c_uint64),
        ("compressed_peak", ctypes.c_uint64), ("compressed_lifetime", ctypes.c_uint64),
        ("phys_footprint", ctypes.c_uint64),
    ]


class _TaskVMInfoRev3(ctypes.Structure):
    # Same prefix, extended to rev3's ledger_phys_footprint_peak (macOS 10.15+), plus room for
    # every later revision: recent kernels (seen on macOS 26) stop at rev2 unless the caller's
    # buffer covers the whole current structure, so ask for more than any kernel returns.
    _pack_ = 4
    _fields_ = _TaskVMInfo._fields_ + [
        ("min_address", ctypes.c_uint64), ("max_address", ctypes.c_uint64),
        ("ledger_phys_footprint_peak", ctypes.c_int64),
        ("_later_revisions", ctypes.c_uint32 * 212),
    ]


_REV3_WORDS = (ctypes.sizeof(_TaskVMInfoRev3) - ctypes.sizeof(ctypes.c_uint32 * 212)) // 4


_LIBC = None


def peak_rss_mb():
    """Highest resident set size of this process so far (macOS reports bytes, Linux KiB)."""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return round(float(usage) / divisor, 1)


def _task_vm_info(structure, needed=None):
    """Fill a task_vm_info structure; None unless the kernel returned the fields we need.

    `needed` is the number of 32-bit words that must be filled (default: the whole structure).
    """
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(ctypes.util.find_library("c"))
    task = ctypes.c_uint.in_dll(_LIBC, "mach_task_self_")
    info = structure()
    wanted = ctypes.sizeof(info) // 4
    count = ctypes.c_uint(wanted)
    result = _LIBC.task_info(task, _TASK_VM_INFO, ctypes.byref(info), ctypes.byref(count))
    if result != 0 or count.value < (wanted if needed is None else needed):
        return None
    return info


def _linux_status_mb(field):
    try:
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(field + ":"):
                    return round(float(line.split()[1]) / 1024.0, 1)
    except OSError:
        return None
    return None


def current_footprint_mb():
    """Current memory as Activity Monitor shows it (phys_footprint); None if unavailable."""
    if sys.platform == "darwin":
        try:
            info = _task_vm_info(_TaskVMInfo)
            return None if info is None else round(float(info.phys_footprint) / 1024.0 / 1024.0, 1)
        except Exception:
            return None
    if sys.platform.startswith("linux"):
        return _linux_status_mb("VmRSS")
    return None


def peak_footprint_mb():
    """Lifetime peak on the same scale as current_footprint_mb(); None if the OS does not say.

    macOS: ledger_phys_footprint_peak. Linux: VmHWM (peak of VmRSS).
    """
    if sys.platform == "darwin":
        try:
            info = _task_vm_info(_TaskVMInfoRev3, needed=_REV3_WORDS)
            if info is None or info.ledger_phys_footprint_peak <= 0:
                return None
            return round(float(info.ledger_phys_footprint_peak) / 1024.0 / 1024.0, 1)
        except Exception:
            return None
    if sys.platform.startswith("linux"):
        return _linux_status_mb("VmHWM")
    return None


def trend_mb_per_hour(samples, minimum=12, edge=6):
    """Slope between the mean of the first and last `edge` samples; samples = [[unix, mb], ...]."""
    points = [(float(t), float(v)) for t, v in samples or [] if v is not None]
    if len(points) < minimum:
        return None
    head, tail = points[:edge], points[-edge:]
    t0 = sum(t for t, _v in head) / len(head)
    t1 = sum(t for t, _v in tail) / len(tail)
    if t1 - t0 < 600:
        return None
    v0 = sum(v for _t, v in head) / len(head)
    v1 = sum(v for _t, v in tail) / len(tail)
    return round((v1 - v0) / ((t1 - t0) / 3600.0), 3)


class _Timeval(ctypes.Structure):
    # <sys/_types/_timeval.h>: time_t tv_sec; suseconds_t (int32) tv_usec — 16 bytes on 64-bit macOS.
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int32)]


_POWER_SYSCTLS = (("boot", b"kern.boottime"), ("sleep", b"kern.sleeptime"), ("wake", b"kern.waketime"))


def power_times():
    """macOS's own record of the last boot, sleep and wake, as unix seconds.

    Returns {"boot": float, "sleep": float|None, "wake": float|None}, or None where the OS
    does not keep this record (anything but macOS) or it cannot be read. sleep/wake are None
    when the Mac has not slept since it booted.
    """
    if sys.platform != "darwin":
        return None
    global _LIBC
    try:
        if _LIBC is None:
            _LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        times = {}
        for key, name in _POWER_SYSCTLS:
            value = _Timeval()
            size = ctypes.c_size_t(ctypes.sizeof(value))
            if _LIBC.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
                return None
            seconds = float(value.tv_sec) + float(value.tv_usec) / 1e6
            times[key] = seconds if seconds > 0 else None
        return times if times.get("boot") else None
    except Exception:
        return None
