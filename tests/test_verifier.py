"""tests/test_verifier.py — 验证器单测（HTTP 重试退避 / 进程 / DB）。"""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from selfheal.verifier import verify, verify_db, verify_http, verify_process

import common
from common import DB_FILE, RUN_DIR, cleanup_state, free_port, kill_mock_processes, start_mock, wait_healthz


def start_flaky_server(fail_until: int = 2, port: int | None = None):
    """返回一个先返回 N 次 503 再返回 200 的 HTTP 服务 (server, url)。"""
    lock = threading.Lock()
    state = {"n": 0}

    class FlakyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                state["n"] += 1
                fail = state["n"] <= fail_until
            code = 503 if fail else 200
            body = b'{"status": "ok"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port or 0), FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/healthz"
    return server, url


def start_alternating_server(port: int | None = None):
    """返回一个 503/200 每请求交替的 HTTP 服务（间歇故障，永远无连续 200）。

    请求序列：503, 200, 503, 200, ...——任何连续 3 次都含一次 503。
    """
    lock = threading.Lock()
    state = {"fail_next": False}

    class AltHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                state["fail_next"] = not state["fail_next"]
                fail = state["fail_next"]
            code = 503 if fail else 200
            body = b'{"status": "ok"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port or 0), AltHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/healthz"
    return server, url


class TestVerifyHttp(unittest.TestCase):
    def test_ok_first_try(self):
        server, url = start_flaky_server(fail_until=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        r = verify_http(url, timeout=2, retries=3, backoff=0.05)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 1)

    def test_retry_until_recovers(self):
        # 前 2 次 503，第 3 次 200 → ok 且 attempts > 1
        server, url = start_flaky_server(fail_until=2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        r = verify_http(url, timeout=2, retries=3, backoff=0.05)
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.attempts, 3)

    def test_always_down(self):
        server, url = start_flaky_server(fail_until=999)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        r = verify_http(url, timeout=2, retries=2, backoff=0.05)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 2)


class TestVerifyHttpSamples(unittest.TestCase):
    """verify_http samples：连续 samples 次 200 才算 ok；间歇 503 永远不过。"""

    def test_stable_succeeds_and_records_samples(self):
        server, url = start_flaky_server(fail_until=0)  # 一直 200
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        r = verify_http(url, timeout=2, retries=1, backoff=0.01,
                        samples=3, sample_interval=0)
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.attempts, 1)
        self.assertEqual(r.samples, 3)

    def test_intermittent_503_never_passes(self):
        # 503/200 交替：任何连续 3 次都含 503 → samples=3 永远不 ok
        server, url = start_alternating_server()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        r = verify_http(url, timeout=2, retries=3, backoff=0.01,
                        samples=3, sample_interval=0)
        self.assertFalse(r.ok)
        self.assertEqual(r.samples, 3)

    def test_one_503_in_window_fails_attempt(self):
        # 前 1 次 503 后稳定 200：首次尝试（samples=3）中第 1 个采样即 503 → 失败；
        # 第 2 次尝试 3 个连续 200 → ok，attempts=2
        server, url = start_flaky_server(fail_until=1)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        r = verify_http(url, timeout=2, retries=2, backoff=0.01,
                        samples=3, sample_interval=0)
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.samples, 3)


class TestVerifyProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cleanup_state()
        cls.port = free_port()
        start_mock(cls.port)
        if not wait_healthz(cls.port):
            raise RuntimeError("mock 服务未就绪")

    @classmethod
    def tearDownClass(cls):
        kill_mock_processes()
        cleanup_state()

    def test_alive(self):
        r = verify_process("python.exe", pid_file=str(RUN_DIR / "mock_svc.pid"))
        self.assertTrue(r.ok, r.detail)

    def test_dead(self):
        r = verify_process("python.exe", pid_file=str(RUN_DIR / "no_such_svc.pid"))
        self.assertFalse(r.ok)


class TestVerifyDb(unittest.TestCase):
    def setUp(self):
        common.run_script("mock_db.py", ["--recreate"])  # 每个用例独立建库

    @classmethod
    def tearDownClass(cls):
        common.run_script("mock_db.py", ["--recreate"])

    def test_healthy(self):
        common.run_script("mock_db.py", ["--init"])
        r = verify_db(str(DB_FILE))
        self.assertTrue(r.ok, r.detail)

    def test_corrupt(self):
        common.run_script("mock_db.py", ["--corrupt"])
        r = verify_db(str(DB_FILE))
        self.assertFalse(r.ok)

    def test_unified_dispatch(self):
        common.run_script("mock_db.py", ["--init"])
        r = verify("db", path=str(DB_FILE))
        self.assertTrue(r.ok, r.detail)


if __name__ == "__main__":
    unittest.main()
