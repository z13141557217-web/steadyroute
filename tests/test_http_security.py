"""v0.4.6: the local dashboard refuses foreign Host headers and sends security headers everywhere."""

import http.client
import pathlib
import socket
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("http_security_router")

PAGES = ("/", "/nodes", "/settings", "/guide", "/changelog", "/assets/pages.css", "/acceptance", "/candidate-acceptance")
APIS = ("/api/status", "/api/v1/status", "/api/nodes")


class HttpSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = router.state_contract.new_state()
        router.update_dashboard_cache(router.build_status_snapshots(state, {}, [], now=1790409600))
        router.update_nodes_cache(state, {"台湾 01": {"type": "Trojan"}}, 1790409600)
        # Serve the repository's dashboard for "/".
        router.DASHBOARD_PATH = str(cycle_harness.MODULE_DIR / "dashboard.html")
        cls.server = router.QuietHTTPServer(("127.0.0.1", 0), router.DashboardHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", "127.0.0.1:%d" % self.port if host is None else host)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_local_hosts_are_served(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port, "LOCALHOST:%d" % self.port):
            with self.subTest(host=host):
                self.assertEqual(self.get("/api/status", host)[0].status, 200)

    def test_foreign_hosts_are_refused_on_every_route(self):
        for host in ("attacker.example:%d" % self.port, "attacker.example", "127.0.0.1", "localhost",
                     "127.0.0.1:1", "localhost.:%d" % self.port, "127.0.0.1.nip.io:%d" % self.port):
            for path in PAGES + APIS:
                with self.subTest(host=host, path=path):
                    response, body = self.get(path, host)
                    self.assertEqual(response.status, 421)
                    self.assertNotIn(b"service", body)

    def test_missing_host_is_refused(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(b"GET /api/status HTTP/1.0\r\n\r\n")
            head = sock.recv(64)
        self.assertTrue(head.startswith(b"HTTP/1.0 421") or head.startswith(b"HTTP/1.1 421"), head)

    def test_every_page_sends_security_headers(self):
        for path in PAGES:
            with self.subTest(path=path):
                response, _ = self.get(path)
                self.assertEqual(response.status, 200)
                csp = response.getheader("Content-Security-Policy") or ""
                self.assertIn("default-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
                self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_apis_are_locked_down(self):
        for path in APIS:
            with self.subTest(path=path):
                response, _ = self.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Security-Policy"), router.API_CSP)
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_unknown_routes_are_plain_404_with_headers(self):
        response, body = self.get("/../state.json")
        self.assertEqual(response.status, 404)
        self.assertEqual(body, b"not found")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")

    def test_host_allowed_unit(self):
        self.assertTrue(router.host_allowed("127.0.0.1:17654", 17654))
        self.assertTrue(router.host_allowed(" localhost:17654 ", 17654))
        self.assertFalse(router.host_allowed("localhost:17655", 17654))
        self.assertFalse(router.host_allowed(None, 17654))
        self.assertFalse(router.host_allowed("evil.localhost:17654", 17654))


if __name__ == "__main__":
    unittest.main()
