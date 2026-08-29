"""tests/test_probe.py — 探针单测（HTTP / 进程 / DB / 磁盘 / 资源）。"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from selfheal.probe import DBProbe, DiskProbe, HTTPProbe, ProcessProbe, ResourceProbe

import common
from common import DB_FILE, RUN_DIR, cleanup_state, free_port, kill_mock_processes, start_mock, wait_healthz


def _spawn_python(code: str):
    """用 base 解释器起独立 python 进程（绕开 hermes shim 双进程）。"""
    exe = getattr(sys, "_base_executable", sys.executable)
    return subprocess.Popen([exe, "-c", code], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _stop_procs(procs) -> None:
    """强杀并回收进程，避免 Popen 未 wait 触发 ResourceWarning。"""
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


class TestHTTPProbe(unittest.TestCase):
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

    def test_healthy(self):
        r = HTTPProbe(f"http://127.0.0.1:{self.port}/healthz", timeout=3).check()
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.status, "ok")
        self.assertGreaterEqual(r.latency_ms, 0)

    def test_500_fails(self):
        r = HTTPProbe(f"http://127.0.0.1:{self.port}/nonexistent", timeout=3).check()
        self.assertFalse(r.ok)  # 404 ≠ 200

    def test_stopped_fails(self):
        kill_mock_processes()
        r = HTTPProbe(f"http://127.0.0.1:{self.port}/healthz", timeout=3).check()
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "network_error")


class TestProcessProbe(unittest.TestCase):
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

    def test_pid_file_alive(self):
        r = ProcessProbe(pid_file=str(RUN_DIR / "mock_svc.pid")).check()
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.status, "alive")

    def test_pid_file_missing(self):
        r = ProcessProbe(pid_file=str(RUN_DIR / "ghost_svc.pid")).check()
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "no_pid_file")


class TestProcessProbeExpectedCount(unittest.TestCase):
    """ProcessProbe pattern + expected_count：单进程 ok / 双进程 duplicate / 缺失。"""

    MARKER = "selfheal_probe_test"

    @classmethod
    def tearDownClass(cls):
        # 兜底清理测试可能残留的进程
        if os.name == "nt":
            subprocess.run(
                ["wmic", "process", "where",
                 f"CommandLine like '%{cls.MARKER}%' and name<>'wmic.exe'",
                 "call", "terminate"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15)

    def _spawn(self, marker: str, count: int) -> list:
        procs = []
        code = f"import time; time.sleep(30)  # {marker}"
        for _ in range(count):
            procs.append(_spawn_python(code))
        time.sleep(0.8)  # 等进程就绪
        return procs

    def test_single_ok(self):
        marker = f"{self.MARKER}_single"
        procs = self._spawn(marker, 1)
        try:
            r = ProcessProbe(pattern=marker, expected_count=1).check()
            self.assertTrue(r.ok, r.detail)
            self.assertEqual(r.status, "alive")
        finally:
            _stop_procs(procs)

    def test_duplicate_fails(self):
        marker = f"{self.MARKER}_dup"
        procs = self._spawn(marker, 2)
        try:
            r = ProcessProbe(pattern=marker, expected_count=1).check()
            self.assertFalse(r.ok)
            self.assertEqual(r.status, "process_duplicate")
        finally:
            _stop_procs(procs)

    def test_missing_fails(self):
        r = ProcessProbe(pattern=f"{self.MARKER}_nonexistent", expected_count=1).check()
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "process_missing")


class TestResourceProbe(unittest.TestCase):
    """ResourceProbe：CPU 打满 / 内存超阈 / 显式 pid 缺失 / 无标记正常。"""

    def test_cpu_spike(self):
        proc = _spawn_python("while True: pass")
        try:
            time.sleep(0.3)
            r = ResourceProbe(pid=proc.pid, cpu_threshold_pct=80,
                              mem_threshold_mb=200, sample_interval=0.3).check()
            self.assertFalse(r.ok)
            self.assertEqual(r.status, "cpu_spike")
        finally:
            _stop_procs([proc])

    def test_mem_high(self):
        proc = _spawn_python("import time; buf = bytearray(300*1024*1024); time.sleep(30)")
        try:
            time.sleep(1.0)  # 等分配完成
            r = ResourceProbe(pid=proc.pid, cpu_threshold_pct=80,
                              mem_threshold_mb=200, sample_interval=0.3).check()
            self.assertFalse(r.ok)
            self.assertEqual(r.status, "mem_high")
        finally:
            _stop_procs([proc])

    def test_missing_fails(self):
        r = ResourceProbe(pid=99999999, sample_interval=0.1).check()
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "process_missing")

    def test_no_marker_ok(self):
        with tempfile.TemporaryDirectory() as d:
            r = ResourceProbe(marker_dir=d, sample_interval=0.1).check()
            self.assertTrue(r.ok)
            self.assertEqual(r.status, "ok")


class TestDBProbe(unittest.TestCase):
    def setUp(self):
        common.run_script("mock_db.py", ["--recreate"])  # 每个用例独立建库

    @classmethod
    def tearDownClass(cls):
        common.run_script("mock_db.py", ["--recreate"])

    def test_normal(self):
        common.run_script("mock_db.py", ["--init"])
        r = DBProbe(str(DB_FILE)).check()
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.status, "ok")

    def test_corrupt_fails(self):
        common.run_script("mock_db.py", ["--corrupt"])
        r = DBProbe(str(DB_FILE)).check()
        self.assertFalse(r.ok)

    def test_missing(self):
        r = DBProbe(str(DB_FILE.parent / "no_such.db")).check()
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "db_missing")


class TestDiskProbe(unittest.TestCase):
    def test_normal_path(self):
        r = DiskProbe(str(common.BASE), threshold_pct=99.9).check()
        self.assertTrue(r.ok, r.detail)

    def test_low_threshold_fails(self):
        r = DiskProbe(str(common.BASE), threshold_pct=0.0001).check()
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "disk_over_threshold")


if __name__ == "__main__":
    unittest.main()
