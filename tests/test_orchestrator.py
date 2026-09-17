"""tests/test_orchestrator.py — mock 环境 run_once 完整生命周期（集成测试）。"""

import subprocess
import sys
import unittest
from pathlib import Path

from selfheal.config import load_services
from selfheal.orchestrator import Orchestrator
from selfheal.policy import PolicyEngine
from selfheal.rule_diagnoser import RuleDiagnoser
from selfheal.state_machine import StateMachine

from common import (BASE, cleanup_state, free_port, kill_mock_processes,
                    start_mock, wait_healthz)

PY = sys.executable
SVC_NAME = "mock_svc"

HEAL_PATH = ["healthy", "degraded", "diagnosing", "policy_check",
             "repairing", "starting", "verifying", "recovered", "healthy"]
ESCALATE_PATH = ["healthy", "degraded", "diagnosing", "policy_check",
                 "escalated", "awaiting_human"]


def run_fault_injector(args):
    return subprocess.run([PY, str(BASE / "mockenv" / "fault_injector.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def make_orchestrator(port: int) -> Orchestrator:
    """按指定端口构造 mock_svc 编排器（快退避，短 cooldown）。"""
    services = load_services(BASE / "config" / "service.yaml",
                             env={"MOCK_PORT": str(port)})
    svc = services[SVC_NAME]
    v = svc.setdefault("verifier", {})
    v["backoff"] = 0.1
    v["retries"] = 2
    sm = StateMachine(SVC_NAME, cooldown_seconds=0)
    orch = Orchestrator(
        services, {SVC_NAME: sm},
        diagnoser_rule=RuleDiagnoser(svc),
        diagnoser_llm=None,
        policy=PolicyEngine(svc),
    )
    return orch


class TestOrchestrator(unittest.TestCase):
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

    def setUp(self):
        # 前一个用例可能已杀掉 mock 进程或注入故障残留：每次用例前
        # 清状态 + 重启一个健康的 mock（端口不变），保证探针从 ok 开始。
        cleanup_state()
        kill_mock_processes()
        start_mock(self.port)
        if not wait_healthz(self.port):
            raise RuntimeError(f"mock 服务未在 {self.port} 就绪")

    def tearDown(self):
        run_fault_injector(["recover", "--all"])

    def test_healthy_no_repair(self):
        orch = make_orchestrator(self.port)
        orch.run_once(SVC_NAME)
        sm = orch.state_machines[SVC_NAME]
        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.state_path, ["healthy"])

    def test_run_once_full_heal_path(self):
        cp = run_fault_injector(["inject", "proc_crash_01"])
        self.assertEqual(cp.returncode, 0, cp.stderr)

        orch = make_orchestrator(self.port)
        results = orch.run_once(SVC_NAME)
        sm = orch.state_machines[SVC_NAME]

        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.state_path, HEAL_PATH)
        # 修复动作 restart，验证通过
        self.assertEqual(results[-1].action, "restart")
        self.assertTrue(results[-1].verified)

    def test_run_once_http500_repaired_by_restart(self):
        # http_500_06：rule 建议 restart@0.8 → 策略门放行；restart 清 state_file →
        # 新进程不再 500 → 恢复。这是"状态型故障可被 restart 修复"的验证。
        cp = run_fault_injector(["inject", "http_500_06"])
        self.assertEqual(cp.returncode, 0, cp.stderr)

        orch = make_orchestrator(self.port)
        results = orch.run_once(SVC_NAME)
        sm = orch.state_machines[SVC_NAME]

        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.state_path, HEAL_PATH)
        self.assertEqual(results[-1].action, "restart")
        self.assertTrue(results[-1].verified)

    def test_run_once_escalate_on_policy_denial(self):
        # http_timeout_08：探针超时(network_error) + 进程在 → unhealthy_but_alive@0.5
        # < 0.7 → 策略门拒绝自动 restart → escalated → awaiting_human
        cp = run_fault_injector(["inject", "http_timeout_08"])
        self.assertEqual(cp.returncode, 0, cp.stderr)

        orch = make_orchestrator(self.port)
        orch.services[SVC_NAME]["probe"]["timeout"] = 2  # 缩短探针超时加速测试
        orch.run_once(SVC_NAME)
        sm = orch.state_machines[SVC_NAME]

        self.assertEqual(sm.state, "awaiting_human")
        self.assertEqual(sm.state_path, ESCALATE_PATH)

    def test_run_once_budget_exhausted(self):
        # 配置损坏：restart 后新进程仍启动失败（配置文件坏的，不是状态文件）→
        # retry 3 次 → 预算耗尽升级人工
        cp = run_fault_injector(["inject", "startup_malformed_10"])
        self.assertEqual(cp.returncode, 0, cp.stderr)

        orch = make_orchestrator(self.port)
        orch.run_once(SVC_NAME)
        sm = orch.state_machines[SVC_NAME]

        self.assertEqual(sm.state, "awaiting_human")
        self.assertEqual(sm.retry_count, 3)
        # 经过 3 轮 repair 路径
        for s in ("repairing", "starting", "retry"):
            self.assertGreaterEqual(sm.state_path.count(s), 3)
        self.assertIn("escalated", sm.state_path)


if __name__ == "__main__":
    unittest.main()
