"""Pure health and scheduling math for SteadyRoute. No I/O, no clocks."""

import math


def percentile(values, fraction):
    """Nearest-rank percentile. values must be non-empty."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    rank = max(1, int(math.ceil(float(fraction) * len(ordered))))
    return ordered[min(len(ordered), rank) - 1]


def optional_percentile(values, fraction, minimum=5):
    """Percentile or None when there are fewer than `minimum` values."""
    values = list(values)
    if len(values) < int(minimum):
        return None
    return percentile(values, fraction)


def next_deadline(previous_deadline, interval, mono_now):
    """Fixed-rate schedule that never bursts to catch up after overrun or sleep."""
    deadline = float(previous_deadline) + float(interval)
    if deadline <= float(mono_now):
        return float(mono_now)
    return deadline


def detect_resume(prev_wall, prev_mono, wall_now, mono_now, interval,
                  last_duration_seconds, slack_seconds=15.0):
    """Return (slept, wall_gap_seconds).

    On macOS time.monotonic() does not advance during system sleep, so a
    wall/monotonic divergence identifies sleep precisely. The gap rule is a
    fallback for platforms whose monotonic clock does advance during sleep.
    """
    if prev_wall is None or prev_mono is None:
        return False, 0
    wall_gap = float(wall_now) - float(prev_wall)
    mono_gap = float(mono_now) - float(prev_mono)
    slept = (wall_gap - mono_gap) > float(slack_seconds) or \
        wall_gap > float(interval) * 3 + float(last_duration_seconds)
    return slept, int(max(0.0, wall_gap))


def count_recent(timestamps, now, window_seconds):
    """Count timestamps within the trailing window."""
    return sum(1 for value in timestamps or [] if float(now) - float(value) < float(window_seconds))


def bounded_append(values, value, limit):
    """Return a new list with value appended and only the last `limit` items kept."""
    items = list(values or [])
    items.append(value)
    return items[-int(limit):]
