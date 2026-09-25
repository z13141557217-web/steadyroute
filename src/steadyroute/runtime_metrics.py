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


_LIBC = None


def peak_rss_mb():
    """Highest resident set size of this process so far (macOS reports bytes, Linux KiB)."""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return round(float(usage) / divisor, 1)


def current_footprint_mb():
    """Current memory as Activity Monitor shows it (phys_footprint); None if unavailable."""
    global _LIBC
    if sys.platform == "darwin":
        try:
            if _LIBC is None:
                _LIBC = ctypes.CDLL(ctypes.util.find_library("c"))
            task = ctypes.c_uint.in_dll(_LIBC, "mach_task_self_")
            info = _TaskVMInfo()
            wanted = ctypes.sizeof(info) // 4
            count = ctypes.c_uint(wanted)
            result = _LIBC.task_info(task, _TASK_VM_INFO, ctypes.byref(info), ctypes.byref(count))
            if result != 0 or count.value < wanted:
                return None
            return round(float(info.phys_footprint) / 1024.0 / 1024.0, 1)
        except Exception:
            return None
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status", "r", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return round(float(line.split()[1]) / 1024.0, 1)
        except OSError:
            return None
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
