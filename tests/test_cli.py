"""tests/test_cli.py — 阶段 5 CLI 出口单测。

子进程方式跑 CLI（python -m selfheal.cli <cmd> --json），验证输出可解析、
起/停 mock 时 status 正确、run 完整生命周期、incidents/audit 空文件与有数据两种情况。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from common import BASE, cleanup_state, free_port, kill_mock_processes, start_mock, wait_healthz

PY = sys.executable


def run_cli(args, env=None, cwd=None):
    merged = dict(os.environ)
    merged["PYTHONIOENCODING"] = "utf-8"
    merged["SELFHEAL_NO_LLM"] = "1"   # 测试不触网，Rule-only
    if env:
        merged.update(env)
    return subprocess.run([PY, "-m", "selfheal.cli", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=180, cwd=cwd or BASE, env=merged)


class TestServices(unittest.TestCase):
    def test_services_lists_mock_and_rag(self):
        cp = run_cli(["services", "--json"])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        names = [s["name"] for s in data]
        self.assertIn("mock_svc", names)
        self.assertIn("rag_qa", names)
        self.assertIn("actions", data[0])

    def test_services_text_output(self):
        cp = run_cli(["services"])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("mock_svc", cp.stdout)
        self.assertIn("rag_qa", cp.stdout)


class TestStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cleanup_state()
        cls.port = free_port()
        cls.env = {"MOCK_PORT": str(cls.port)}

    def setUp(self):
        kill_mock_processes()
        cleanup_state()
        start_mock(self.port)
        self.assertTrue(wait_healthz(self.port), "mock 未就绪")

    def tearDown(self):
        kill_mock_processes()
        cleanup_state()

    def test_status_ok_then_fail(self):
        cp = run_cli(["status", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["service"], "mock_svc")
        self.assertTrue(data[0]["ok"])
        self.assertTrue(any(c["type"] == "probe(http)" for c in data[0]["checks"]))

        # 杀掉 mock → status 应显示 [FAIL]
        kill_mock_processes()
        cp = run_cli(["status", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertFalse(data[0]["ok"])
        self.assertIn("network_error",
                      [c["status"] for c in data[0]["checks"] if c["type"] == "probe(http)"])

    def test_status_all_services_parseable(self):
        cp = run_cli(["status", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertGreaterEqual(len(data), 2)  # mock_svc + rag_qa

    def test_status_unknown_service(self):
        cp = run_cli(["status", "nope", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 2)
        self.assertIn("未知服务", cp.stderr)


class TestVerify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cleanup_state()
        cls.port = free_port()
        cls.env = {"MOCK_PORT": str(cls.port)}

    def setUp(self):
        kill_mock_processes()
        cleanup_state()
        start_mock(self.port)
        self.assertTrue(wait_healthz(self.port))

    def tearDown(self):
        kill_mock_processes()
        cleanup_state()

    def test_verify_healthy_mock(self):
        cp = run_cli(["verify", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("HTTP 200", data["detail"])

    def test_verify_down_service(self):
        kill_mock_processes()
        cp = run_cli(["verify", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertFalse(data["ok"])


class TestRun(unittest.TestCase):
    """run 命令：起 mock → 注入 proc_crash → run → 恢复 healthy。"""

    @classmethod
    def setUpClass(cls):
        cleanup_state()
        cls.port = free_port()
        cls.env = {"MOCK_PORT": str(cls.port)}

    def setUp(self):
        kill_mock_processes()
        cleanup_state()
        start_mock(self.port)
        self.assertTrue(wait_healthz(self.port))

    def tearDown(self):
        subprocess.run([PY, str(BASE / "mockenv" / "fault_injector.py"), "recover", "--all"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
        kill_mock_processes()
        cleanup_state()

    def test_run_healthy_service_noop(self):
        cp = run_cli(["run", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertEqual(data["final_state"], "healthy")
        self.assertTrue(data["ok"])

    def test_run_full_heal_after_crash(self):
        cp = run_cli(["status", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)

        inj = subprocess.run([PY, str(BASE / "mockenv" / "fault_injector.py"),
                              "inject", "proc_crash_01"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
        self.assertEqual(inj.returncode, 0, inj.stderr)

        # 注入后 status 应为 FAIL
        cp = run_cli(["status", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertFalse(json.loads(cp.stdout)[0]["ok"])

        # run 完整生命周期 → 回到 healthy
        with tempfile.TemporaryDirectory() as td:
            env = dict(self.env)
            env["SELFHEAL_AUDIT_DIR"] = td
            cp = run_cli(["run", "mock_svc", "--json"], env=env)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            data = json.loads(cp.stdout)
            self.assertEqual(data["final_state"], "healthy")
            self.assertTrue(data["ok"])
            self.assertEqual(data["action"], "restart")
            self.assertTrue(data["verified"])
            self.assertIn("recovered", data["state_path"])
            self.assertTrue(data["incident_id"])
            # Incident 已落到覆盖的审计目录
            inc_file = Path(td) / "incidents.jsonl"
            self.assertTrue(inc_file.exists())
            self.assertIn("mock_svc", inc_file.read_text(encoding="utf-8"))

        # run 结束后 status 恢复 OK
        cp = run_cli(["status", "mock_svc", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue(json.loads(cp.stdout)[0]["ok"])


class TestIncidentsAudit(unittest.TestCase):
    """incidents/audit 命令：空文件与有数据两种情况（用临时审计目录隔离）。"""

    def _write(self, td, name, lines):
        p = Path(td) / name
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
                     encoding="utf-8")
        return p

    def test_incidents_empty(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "incidents.jsonl").write_text("", encoding="utf-8")
            cp = run_cli(["incidents", "--json"], env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(json.loads(cp.stdout), [])

    def test_incidents_with_data_sorted_desc(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [
                {"service": "mock_svc", "incident_id": "aaaa", "state_path": ["healthy", "recovered"],
                 "action": "restart", "ok": True,
                 "started_at": "2026-08-30T10:00:00", "ended_at": "2026-08-30T10:00:02"},
                {"service": "mock_svc", "incident_id": "bbbb", "state_path": ["healthy", "awaiting_human"],
                 "action": "escalate", "ok": False,
                 "started_at": "2026-08-30T11:00:00", "ended_at": "2026-08-30T11:00:01"},
            ]
            self._write(td, "incidents.jsonl", lines)
            cp = run_cli(["incidents", "--last", "5", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            data = json.loads(cp.stdout)
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["incident_id"], "bbbb")  # 按时间倒序
            self.assertTrue(data[1]["ok"])
            self.assertFalse(data[0]["ok"])

    def test_incidents_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            cp = run_cli(["incidents", "--json"], env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(json.loads(cp.stdout), [])

    def test_audit_empty(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "policy.jsonl").write_text("", encoding="utf-8")
            cp = run_cli(["audit", "--json"], env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(json.loads(cp.stdout), [])

    def test_audit_with_data(self):
        with tempfile.TemporaryDirectory() as td:
            lines = [
                {"ts": "2026-08-30T10:00:00", "service": "mock_svc", "action": "restart",
                 "confidence": 0.9, "allowed": True, "reason": "允许"},
                {"ts": "2026-08-30T11:00:00", "service": "mock_svc", "action": "restart",
                 "confidence": 0.6, "allowed": False, "reason": "置信度低，拒绝"},
            ]
            self._write(td, "policy.jsonl", lines)
            cp = run_cli(["audit", "--last", "5", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            data = json.loads(cp.stdout)
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["ts"], "2026-08-30T11:00:00")  # 倒序
            self.assertFalse(data[0]["allowed"])
            self.assertIn("allowed", data[0])

    def test_incidents_text_output(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "incidents.jsonl", [
                {"service": "mock_svc", "incident_id": "cccccccc-0000",
                 "state_path": ["healthy", "degraded", "recovered"],
                 "action": "restart", "ok": True,
                 "started_at": "2026-08-30T10:00:00", "ended_at": "2026-08-30T10:00:01"},
            ])
            cp = run_cli(["incidents"], env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn("#cccccccc", cp.stdout)
            self.assertIn("已恢复", cp.stdout)
            self.assertIn("healthy → degraded → recovered", cp.stdout)


class TestIncidentShow(unittest.TestCase):
    """incident <id>：合并 incidents.jsonl + policy.jsonl 回放决策链。"""

    def _setup(self, td):
        Path(td, "incidents.jsonl").write_text(
            json.dumps({"service": "mock_svc", "incident_id": "abc12345-0000",
                        "state_path": ["healthy", "degraded", "diagnosing",
                                       "policy_check", "repairing", "recovered"],
                        "action": "restart", "ok": True,
                        "started_at": "2026-08-30T10:00:00",
                        "ended_at": "2026-08-30T10:00:03"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        Path(td, "policy.jsonl").write_text(
            json.dumps({"schema_version": 1, "ts": "2026-08-30T10:00:01",
                        "service": "mock_svc", "incident_id": "abc12345-0000",
                        "action": "restart", "confidence": 0.9, "allowed": True,
                        "reason": "允许"}, ensure_ascii=False) + "\n",
            encoding="utf-8")

    def test_show_full_id(self):
        with tempfile.TemporaryDirectory() as td:
            self._setup(td)
            cp = run_cli(["incident", "abc12345-0000", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            data = json.loads(cp.stdout)
            self.assertEqual(data["incident"]["incident_id"], "abc12345-0000")
            self.assertEqual(len(data["policy_chain"]), 1)
            self.assertTrue(data["policy_chain"][0]["allowed"])

    def test_show_prefix_match(self):
        with tempfile.TemporaryDirectory() as td:
            self._setup(td)
            cp = run_cli(["incident", "abc12345", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(json.loads(cp.stdout)["incident"]["incident_id"],
                             "abc12345-0000")

    def test_show_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            self._setup(td)
            cp = run_cli(["incident", "zzz999", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 2)
            self.assertIn("未找到", cp.stderr)

    def test_show_text_output(self):
        with tempfile.TemporaryDirectory() as td:
            self._setup(td)
            cp = run_cli(["incident", "abc12345-0000"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn("策略门决策链", cp.stdout)
            self.assertIn("[ALLOW]", cp.stdout)


class TestLogsCommand(unittest.TestCase):
    """logs 命令：读服务日志。"""

    def test_logs_no_log_file(self):
        with tempfile.TemporaryDirectory() as td:
            cp = run_cli(["logs", "mock_svc", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            data = json.loads(cp.stdout)
            self.assertIsInstance(data, list)

    def test_logs_tail(self):
        with tempfile.TemporaryDirectory() as td:
            mockenv_log = Path(td) / "mock_svc.log"
            mockenv_log.write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n",
                                   encoding="utf-8")
            # 用 SELFHEAL_LOG_DIR 覆盖 mock 日志目录
            cp = run_cli(["logs", "mock_svc", "--tail", "3", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td, "SELFHEAL_LOG_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            data = json.loads(cp.stdout)
            self.assertEqual(len(data), 3)
            self.assertIn("line 10", data[-1]["line"])


class TestRunDryRun(unittest.TestCase):
    """run --dry-run：只诊断 + 策略门判定，不执行动作。"""

    @classmethod
    def setUpClass(cls):
        from common import cleanup_state, free_port
        cleanup_state()
        cls.port = free_port()
        cls.env = {"MOCK_PORT": str(cls.port)}

    def setUp(self):
        from common import kill_mock_processes, start_mock
        kill_mock_processes()
        start_mock(self.port)
        self.assertTrue(wait_healthz(self.port))

    def tearDown(self):
        from common import kill_mock_processes
        kill_mock_processes()

    def test_dry_run_healthy_reports_policy(self):
        cp = run_cli(["run", "mock_svc", "--dry-run", "--json"], env=self.env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        data = json.loads(cp.stdout)
        self.assertTrue(data["dry_run"])
        self.assertIn("policy_decisions", data)
        # 健康服务：诊断可能有建议，但 dry-run 不执行
        self.assertIn("未执行任何动作", data["note"])

    def test_dry_run_does_not_write_incident(self):
        with tempfile.TemporaryDirectory() as td:
            cp = run_cli(["run", "mock_svc", "--dry-run", "--json"],
                         env={**self.env, "SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            inc_file = Path(td) / "incidents.jsonl"
            # dry-run 不应产生 Incident 记录
            self.assertFalse(inc_file.exists())


class TestJsonErrorEnvelope(unittest.TestCase):
    """--json 模式下错误输出 JSON envelope + 非 0 退出码。"""

    def test_status_unknown_service_json_envelope(self):
        cp = run_cli(["status", "nope", "--json"])
        self.assertEqual(cp.returncode, 2)
        data = json.loads(cp.stderr)
        self.assertFalse(data["ok"])
        self.assertIn("error", data)
        self.assertIn("未知服务", data["error"]["message"])

    def test_incident_not_found_json_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            cp = run_cli(["incident", "nope-id", "--json"],
                         env={"SELFHEAL_AUDIT_DIR": td})
            self.assertEqual(cp.returncode, 2)
            data = json.loads(cp.stderr)
            self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
