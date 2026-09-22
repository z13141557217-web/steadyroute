import importlib.util
import io
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src" / "steadyroute"))
import runtime_logging

SPEC = importlib.util.spec_from_file_location("logging_router", str(ROOT / "src" / "steadyroute" / "weighted_router.py"))
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


class BoundedLoggingTests(unittest.TestCase):
    def tearDown(self):
        if runtime_logging._active is not None:
            runtime_logging._active.close()

    def test_main_and_error_logs_rotate_and_new_writes_use_new_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            context = runtime_logging.install(directory, main_max_bytes=160, main_backups=2,
                                              error_max_bytes=120, error_backups=1)
            try:
                runtime_logging.info("initial " + "a" * 50)
                first_inode = (pathlib.Path(directory) / "router.log").stat().st_ino
                for index in range(12):
                    runtime_logging.info("message %02d %s" % (index, "x" * 30))
                    print("stderr %02d %s" % (index, "y" * 20), file=sys.stderr)
                runtime_logging.info("LATEST-MAIN")
                print("LATEST-ERROR", file=sys.stderr)
                self.assertNotEqual(first_inode, (pathlib.Path(directory) / "router.log").stat().st_ino)
            finally:
                context.close()
            main = pathlib.Path(directory) / "router.log"
            error = pathlib.Path(directory) / "router-error.log"
            self.assertIn("LATEST-MAIN", main.read_text())
            self.assertIn("LATEST-ERROR", error.read_text())
            self.assertEqual(main.stat().st_mode & 0o777, 0o600)
            self.assertEqual(error.stat().st_mode & 0o777, 0o600)
            self.assertLessEqual(sum(p.stat().st_size for p in pathlib.Path(directory).glob("router.log*")), 160 * 3)
            self.assertLessEqual(sum(p.stat().st_size for p in pathlib.Path(directory).glob("router-error.log*")), 120 * 2)

    def test_oversized_preupgrade_log_is_bounded_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            old = pathlib.Path(directory) / "router.log"
            old.write_bytes(b"a" * 2000 + b"recent\n")
            first_archive = pathlib.Path(directory) / "router.log.1"
            first_archive.write_bytes(b"b" * 2000)
            excess_archive = pathlib.Path(directory) / "router.log.3"
            excess_archive.write_bytes(b"old archive")
            context = runtime_logging.install(directory, main_max_bytes=160, main_backups=2,
                                              error_max_bytes=120, error_backups=1)
            context.close()
            self.assertLessEqual(old.stat().st_size, 160)
            self.assertLessEqual(first_archive.stat().st_size, 160)
            self.assertFalse(excess_archive.exists())
            self.assertIn(b"recent", old.read_bytes())

    def test_uncaught_main_and_thread_errors_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            context = runtime_logging.install(directory, main_max_bytes=2048, main_backups=1,
                                              error_max_bytes=4096, error_backups=1)
            try:
                try:
                    raise ValueError("main sentinel")
                except ValueError:
                    context._uncaught(*sys.exc_info())
                try:
                    raise RuntimeError("thread sentinel")
                except RuntimeError:
                    kind, value, traceback = sys.exc_info()
                    context._thread_uncaught(types.SimpleNamespace(
                        thread=types.SimpleNamespace(name="test-worker"),
                        exc_type=kind, exc_value=value, exc_traceback=traceback,
                    ))
            finally:
                context.close()
            content = (pathlib.Path(directory) / "router-error.log").read_text()
            self.assertIn("main sentinel", content)
            self.assertIn("thread sentinel", content)
            self.assertIn("test-worker", content)

    def test_probe_detail_is_capped_but_failure_change_is_immediate(self):
        router.LAST_PROBE_DETAIL_LOG_AT = None
        router.LAST_PROBE_FAILURES = None
        with mock.patch.object(router, "log") as log:
            router.log_probe_results(["node=100ms"], [], [], 1000)
            router.log_probe_results(["node=90ms"], [], [], 1020)
            router.log_probe_results(["node=FAIL"], [], ["node"], 1040)
            router.log_probe_results(["node=FAIL"], [], ["node"], 1060)
            router.log_probe_results(["node=80ms"], [], [], 1080)
            router.log_probe_results(["node=80ms"], [], [], 1600)
        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(sum(message.startswith("probe: ") for message in messages), 2)
        self.assertIn("probe failure changed: node", messages)
        self.assertIn("probe failures recovered", messages)


class DashboardDisconnectTests(unittest.TestCase):
    def make_handler(self, write_side_effect):
        handler = object.__new__(router.DashboardHandler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = types.SimpleNamespace(write=mock.Mock(side_effect=write_side_effect))
        return handler

    def test_only_expected_client_disconnects_are_swallowed_at_write_boundary(self):
        for exception in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            with self.subTest(exception=exception):
                handler = self.make_handler(exception("client gone"))
                handler.write_response(200, "text/plain", b"hello")
                handler = self.make_handler(None)
                handler.send_header.side_effect = exception("client gone during headers")
                handler.write_response(200, "text/plain", b"hello")
        handler = self.make_handler(ValueError("real error"))
        with self.assertRaisesRegex(ValueError, "real error"):
            handler.write_response(200, "text/plain", b"hello")

    def test_disconnect_from_non_response_logic_is_not_swallowed(self):
        handler = self.make_handler(None)
        handler.path = "/candidate-acceptance"
        with mock.patch.object(router, "static_acceptance_response", side_effect=ConnectionResetError("not response")):
            with self.assertRaises(ConnectionResetError):
                handler.do_GET()

    def test_unhandled_http_exception_is_logged(self):
        server = object.__new__(router.DashboardServer)
        with mock.patch.object(router.runtime_logging, "exception") as log_error:
            try:
                raise ValueError("unexpected handler bug")
            except ValueError:
                server.handle_error(None, None)
        log_error.assert_called_once_with("unhandled dashboard request exception")


if __name__ == "__main__":
    unittest.main()
