"""tests/test_executor.py — 执行器单测（restart 幂等 / 白名单 / escalate audit）。"""

import json
import tempfile
import unittest
from pathlib import Path

from selfheal import executor
from selfheal.config import load_services

import common
from common import cleanup_state, free_port, kill_mock_processes, wait_healthz

CONFIG = common.BASE / "config" / "service.yaml"


class TestRestartIdempotent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cleanup_state()
        kill_mock_processes()
        cls.port = free_port()
        cls.services = load_services(CONFIG, env={"MOCK_PORT": str(cls.port)})
        cls.svc = cls.services["mock_svc"]

    @classmethod
    def tearDownClass(cls):
        kill_mock_processes()
        cleanup_state()
        executor.AUDIT_LOG.unlink(missing_ok=True)

    def test_restart_idempotent(self):
        # 第一次重启：启动服务
        r1 = executor.execute("restart", self.svc)
        self.assertTrue(r1.ok, r1.detail)
        self.assertTrue(wait_healthz(self.port), "重启后服务未就绪")

        # 第二次重启：先杀后启，幂等成功
        r2 = executor.execute("restart", self.svc)
        self.assertTrue(r2.ok, r2.detail)
        self.assertTrue(wait_healthz(self.port), "二次重启后服务未就绪")

        # 第三次重启：仍成功
        r3 = executor.execute("restart", self.svc)
        self.assertTrue(r3.ok, r3.detail)
        self.assertTrue(wait_healthz(self.port), "三次重启后服务未就绪")

    def test_kill_stale_no_residual(self):
        # 先确保 mock 在跑，kill_stale 应杀掉它且返回 ok
        r = executor.execute("kill_stale", self.svc)
        self.assertTrue(r.ok, r.detail)
        # 再跑一次，无残留也应 ok（幂等）
        r2 = executor.execute("kill_stale", self.svc)
        self.assertTrue(r2.ok, r2.detail)

    def test_restart_clears_state_file(self):
        # restart 前应删除 state_file（清运行时状态，模拟"重启清内存"）
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "mock_svc.json"
            state_file.write_text('{"modes": ["http_500"]}', encoding="utf-8")
            svc = dict(self.svc)                 # 浅拷贝，不动共享配置
            svc["state_file"] = str(state_file)
            r = executor.execute("restart", svc)
            self.assertTrue(r.ok, r.detail)
            self.assertFalse(state_file.exists(), "restart 应删除 state_file")
            # 幂等：文件已删，再重启也成功（缺失无害）
            r2 = executor.execute("restart", svc)
            self.assertTrue(r2.ok, r2.detail)
            self.assertFalse(state_file.exists())

    def test_unknown_action_rejected(self):
        r = executor.execute("rm_rf", self.svc)
        self.assertFalse(r.ok)
        self.assertIn("白名单", r.detail)


class TestOtherActions(unittest.TestCase):
    def test_cleanup_logs_removes_big_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.log"
            small = Path(tmp) / "small.log"
            big.write_bytes(b"x" * (2 * 1024 * 1024))
            small.write_bytes(b"x")
            svc = {"display": "t", "log_dir": tmp}
            r = executor.execute("cleanup_logs", svc)
            self.assertTrue(r.ok, r.detail)
            self.assertFalse(big.exists(), "大文件应被删除")
            self.assertTrue(small.exists(), "小文件应保留")

    def test_git_revert_not_configured(self):
        services = load_services(CONFIG)
        r = executor.execute("git_revert_config", services["mock_svc"])  # config_dir=null
        self.assertFalse(r.ok)
        self.assertIn("未配置", r.detail)

    def test_escalate_writes_audit(self):
        services = load_services(CONFIG)
        svc = services["mock_svc"]
        executor.AUDIT_LOG.unlink(missing_ok=True)
        r = executor.execute("escalate", svc, reason="单测升级")
        self.assertTrue(r.ok, r.detail)
        line = executor.AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()[-1]
        entry = json.loads(line)
        self.assertEqual(entry["action"], "escalate")
        self.assertEqual(entry["reason"], "单测升级")
        executor.AUDIT_LOG.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
