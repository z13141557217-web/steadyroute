"""Measure client-visible recovery from independent requests in a controlled drill."""
import http.client
import time
import uuid
from urllib.parse import urlsplit


def proxy_http_probe(port, url, expected_body, timeout=1.0):
    """Send a fresh HTTP request through the local test proxy, bypassing caches."""
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.fragment or parsed.username:
        raise ValueError("provide a controlled HTTP endpoint without credentials or fragment")
    if not expected_body or len(expected_body) > 4096:
        raise ValueError("provide a short, distinctive response body")

    def probe():
        separator = "&" if parsed.query else "?"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request("GET", url + separator + "probe=" + uuid.uuid4().hex,
                               headers={"Cache-Control": "no-cache, no-store", "Connection": "close"})
            response = connection.getresponse()
            return response.status == 200 and response.read(len(expected_body) + 1) == expected_body
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    return probe


def measure_recovery(probe, fault_at, *, interval=0.1, timeout=10.0):
    """Use the fault injector's monotonic timestamp on the same host."""
    deadline = fault_at + timeout
    attempts = 0
    last_failure_ms = None
    while time.monotonic() < deadline:
        attempts += 1
        ok = probe()
        finished = time.monotonic()
        elapsed_ms = round((finished - fault_at) * 1000)
        if ok and finished <= deadline:
            return {"attempts": attempts, "last_failure_ms": last_failure_ms,
                    "first_success_ms": elapsed_ms, "outage_observed": last_failure_ms is not None}
        if not ok:
            last_failure_ms = elapsed_ms
        time.sleep(max(0, min(interval, deadline - finished)))
    return {"attempts": attempts, "last_failure_ms": last_failure_ms,
            "first_success_ms": None, "outage_observed": last_failure_ms is not None}
