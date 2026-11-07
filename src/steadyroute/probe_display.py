"""Bounded, uniform five-second chart buckets, independent of routing decisions."""
import collections
import threading

INTERVAL = 5
WINDOW = 1830
LIMIT = 800
_groups = {}
_lock = threading.Lock()


def reset():
    with _lock:
        _groups.clear()


def _apply(bucket, delay, status, elapsed, details, confirmation):
    bucket["counts"][status] += 1
    bucket["status"] = status
    bucket["elapsed"] = elapsed
    bucket["details"] = details
    if status == "failed":
        bucket["last_failure"] = details
        bucket["reasons"][(details or {}).get("status", "unknown")] += 1
        bucket["retry_recovered"] += confirmation == "transient"
    if status == "success" and delay is not None:
        bucket["latest_success"] = delay
        bucket["min"] = delay if bucket["min"] is None else min(bucket["min"], delay)
        bucket["max"] = delay if bucket["max"] is None else max(bucket["max"], delay)


def record(group, node, at, delay, request_id, status, confirmation, elapsed, details):
    key = (int(at // INTERVAL) * INTERVAL, node)
    with _lock:
        buckets = _groups.setdefault(group, collections.OrderedDict())
        if key not in buckets:
            buckets[key] = dict(t=key[0], node=node, counts=collections.Counter(), pending={},
                                status=status, latest_success=None, min=None, max=None,
                                confirmation=confirmation, elapsed=elapsed, details=details,
                                last_failure=None, reasons=collections.Counter(), retry_recovered=0)
        bucket = buckets[key]
        if status == "pending":
            bucket["pending"][request_id] = confirmation
        _apply(bucket, delay, status, elapsed, details, confirmation)
        for old in list(buckets):
            if old[0] < at - WINDOW:
                del buckets[old]
        while len(buckets) > LIMIT:
            buckets.popitem(last=False)


def complete(group, node, at, request_id, delay, status, elapsed, details):
    key = (int(at // INTERVAL) * INTERVAL, node)
    with _lock:
        bucket = _groups.get(group, {}).get(key)
        if bucket is None or request_id not in bucket["pending"]:
            return
        confirmation = bucket["pending"].pop(request_id)
        bucket["counts"]["pending"] -= 1
        _apply(bucket, delay, status, elapsed, details, confirmation)


def snapshot(group, now):
    with _lock:
        result = []
        for bucket in _groups.get(group, {}).values():
            if bucket["t"] < now - WINDOW:
                continue
            counts = dict(bucket["counts"])
            # A mixed bucket reports its successful samples, but breaks the main line.
            broken = (counts.get("failed", 0) or counts.get("cancelled", 0)
                      or counts.get("pending", 0) or counts.get("unavailable", 0))
            delay = None if broken else bucket["latest_success"]
            info = dict(counts=counts, min_ms=bucket["min"], max_ms=bucket["max"],
                        latest_success_ms=bucket["latest_success"], interval_seconds=INTERVAL,
                        failure_reasons=dict(bucket["reasons"]))
            status = ("pending" if counts.get("pending") else "failed" if counts.get("failed")
                      else "unavailable" if counts.get("unavailable")
                      else "cancelled" if counts.get("cancelled") else "success")
            confirmation = "transient" if counts.get("failed") and bucket["retry_recovered"] == counts["failed"] else None
            result.append([bucket["t"], delay, "probe", bucket["node"], None,
                           status, confirmation, bucket["elapsed"],
                           None, bucket["last_failure"] if counts.get("failed") else bucket["details"], info])
        return sorted(result, key=lambda point: (point[0], point[3]))
