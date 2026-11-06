import http.server
import pathlib
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "sim" / "detection"))
from measure_recovery import measure_recovery, proxy_http_probe


class RecoveryProbeTests(unittest.TestCase):
    def test_independent_http_requests_detect_failure_then_verified_success(self):
        requests = []

        class ProxyFixture(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(503 if len(requests) == 1 else 200)
                self.end_headers()
                self.wfile.write(b"recovered")

            def log_message(self, *args):
                pass

        with http.server.HTTPServer(("127.0.0.1", 0), ProxyFixture) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                probe = proxy_http_probe(server.server_port, "http://fixture.invalid/check", b"recovered")
                result = measure_recovery(probe, time.monotonic(), interval=0.01, timeout=2)
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["outage_observed"])
        self.assertGreater(result["first_success_ms"], result["last_failure_ms"])
        self.assertEqual(len(set(requests)), 2)
