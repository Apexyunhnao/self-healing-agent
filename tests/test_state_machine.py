"""tests/test_state_machine.py — 状态机转移表全覆盖。"""

import time
import unittest

from selfheal.state_machine import MAX_RETRIES, StateMachine

HEAL_PATH = ["healthy", "degraded", "diagnosing", "policy_check",
             "repairing", "starting", "verifying", "recovered", "healthy"]


def to_diagnosing(sm: StateMachine) -> None:
    """healthy →(2 次失败)→ degraded →(第 3 次)→ diagnosing。"""
    sm.event("probe_fail")            # healthy, pending=1
    assert sm.state == "healthy"
    sm.event("probe_fail")            # pending=2 → degraded
    assert sm.state == "degraded"
    sm.event("probe_fail")            # pending=3 → diagnosing
    assert sm.state == "diagnosing"


class TestHealthyDegraded(unittest.TestCase):
    def test_probe_ok_keeps_healthy(self):
        sm = StateMachine("svc")
        sm.event("probe_ok")
        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.pending_failures, 0)

    def test_first_fail_only_counts(self):
        sm = StateMachine("svc")
        sm.event("probe_fail")
        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.pending_failures, 1)

    def test_second_fail_enters_degraded(self):
        sm = StateMachine("svc")
        sm.event("probe_fail")
        sm.event("probe_fail")
        self.assertEqual(sm.state, "degraded")

    def test_degraded_probe_ok_recovers(self):
        sm = StateMachine("svc")
        sm.event("probe_fail")
        sm.event("probe_fail")
        sm.event("probe_ok")
        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.pending_failures, 0)

    def test_third_fail_enters_diagnosing(self):
        sm = StateMachine("svc")
        sm.event("probe_fail")
        sm.event("probe_fail")
        sm.event("probe_fail")
        self.assertEqual(sm.state, "diagnosing")

    def test_state_path_starts_healthy(self):
        sm = StateMachine("svc")
        self.assertEqual(sm.state_path, ["healthy"])
        sm.event("probe_fail")
        sm.event("probe_fail")
        self.assertEqual(sm.state_path, ["healthy", "degraded"])


class TestRepairFlow(unittest.TestCase):
    def test_full_heal_path(self):
        sm = StateMachine("svc", cooldown_seconds=0)
        to_diagnosing(sm)
        sm.event("diagnosis_ready")    # policy_check
        sm.event("allowed")            # repairing
        sm.event("started")            # starting
        sm.event("ready")              # verifying
        sm.event("pass")               # recovered
        self.assertEqual(sm.state, "recovered")
        sm.event("cooldown_done")
        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.state_path, HEAL_PATH)

    def test_execute_fail_goes_retry(self):
        sm = StateMachine("svc")
        to_diagnosing(sm)
        sm.event("diagnosis_ready")
        sm.event("allowed")
        sm.event("fail")
        self.assertEqual(sm.state, "retry")
        self.assertEqual(sm.retry_count, 1)

    def test_starting_timeout_goes_retry(self):
        sm = StateMachine("svc")
        to_diagnosing(sm)
        sm.event("diagnosis_ready")
        sm.event("allowed")
        sm.event("started")
        sm.event("timeout")
        self.assertEqual(sm.state, "retry")

    def test_retry_budget_exhausted_escalates(self):
        sm = StateMachine("svc")
        to_diagnosing(sm)
        sm.event("diagnosis_ready")
        sm.event("allowed")
        for _ in range(MAX_RETRIES - 1):
            sm.event("started")
            sm.event("timeout")        # retry
            sm.event("budget_left")    # repairing
        sm.event("started")
        sm.event("timeout")            # 第 3 次失败
        self.assertEqual(sm.state, "retry")
        self.assertEqual(sm.retry_count, MAX_RETRIES)
        sm.event("budget_exhausted")
        self.assertEqual(sm.state, "escalated")

    def test_escalate_then_awaiting_human_then_fixed(self):
        sm = StateMachine("svc")
        to_diagnosing(sm)
        sm.event("no_diagnosis")       # escalated
        self.assertEqual(sm.state, "escalated")
        sm.event("human_ack")
        self.assertEqual(sm.state, "awaiting_human")
        sm.event("service_fixed")
        self.assertEqual(sm.state, "healthy")

    def test_repair_lock(self):
        sm = StateMachine("svc")
        self.assertFalse(sm.repair_lock)
        to_diagnosing(sm)
        sm.event("diagnosis_ready")
        sm.event("allowed")
        self.assertTrue(sm.repair_lock)      # repairing
        sm.event("started")
        self.assertTrue(sm.repair_lock)      # starting
        sm.event("ready")
        self.assertTrue(sm.repair_lock)      # verifying
        sm.event("pass")
        self.assertFalse(sm.repair_lock)     # recovered


class TestQuarantine(unittest.TestCase):
    def test_second_escalation_quarantines(self):
        sm = StateMachine("svc")
        # 第一次 escalated
        to_diagnosing(sm)
        sm.event("no_diagnosis")             # escalated (count=1)
        self.assertEqual(sm.state, "escalated")
        sm.event("human_ack")                # awaiting_human
        sm.event("service_fixed")            # healthy
        self.assertEqual(sm.state, "healthy")
        # 第二次 escalated → 自动 quarantine
        to_diagnosing(sm)
        sm.event("no_diagnosis")             # escalated → count=2 → quarantined
        self.assertEqual(sm.state, "quarantined")
        self.assertIn("quarantined", sm.state_path)

    def test_explicit_quarantine_event(self):
        sm = StateMachine("svc")
        sm.event("quarantine")
        self.assertEqual(sm.state, "quarantined")


class TestCooldown(unittest.TestCase):
    def test_cooldown_gate(self):
        sm = StateMachine("svc", cooldown_seconds=30)
        to_diagnosing(sm)
        sm.event("diagnosis_ready")
        sm.event("allowed")
        sm.event("started")
        sm.event("ready")
        sm.event("pass")
        self.assertEqual(sm.state, "recovered")
        self.assertGreater(sm.cooldown_until, time.time())
        sm.event("cooldown_done")
        self.assertEqual(sm.state, "recovered")   # 冷却未到，不转移
        sm.cooldown_until = time.time() - 1
        sm.event("cooldown_done")
        self.assertEqual(sm.state, "healthy")


class TestInvalid(unittest.TestCase):
    def test_invalid_transition_raises(self):
        sm = StateMachine("svc")
        with self.assertRaises(ValueError):
            sm.event("diagnosis_ready")          # healthy 无此转移

    def test_quarantine_from_quarantined_raises(self):
        sm = StateMachine("svc")
        sm.event("quarantine")
        with self.assertRaises(ValueError):
            sm.event("quarantine")


if __name__ == "__main__":
    unittest.main()
