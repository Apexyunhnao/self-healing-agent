"""tests/test_policy.py — 策略门单测。"""

import json
import tempfile
import unittest
from pathlib import Path

from selfheal.models import DiagnosisResult
from selfheal.policy import PolicyEngine

MOCK_SVC = {
    "display": "Mock 测试服务",
    "process": {"pattern": "mock_server.py"},
    "repair": {"actions": ["restart", "kill_stale", "cleanup_logs", "escalate"]},
}
RAG_QA = {
    "display": "RAG 知识库（真实服务，只读探针）",
    "process": {"pattern": "uvicorn"},
    "repair": {"actions": ["escalate"]},
}
NO_PATTERN = {
    "display": "无进程 pattern",
    "process": {},
    "repair": {"actions": ["restart", "kill_stale", "escalate"]},
}


def diag(action: str, conf: float = 0.9) -> DiagnosisResult:
    return DiagnosisResult("root", conf, action, "reason", "rule")


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Path(self.tmp.name) / "policy.jsonl"

    def _make(self, cfg):
        return PolicyEngine(cfg, audit_path=self.audit)

    def test_whitelist_deny(self):
        p = self._make(MOCK_SVC)
        allowed, reason = p.check("git_revert_config", 0.9, diag("git_revert_config"))
        self.assertFalse(allowed)
        self.assertIn("不在白名单", reason)

    def test_escalate_always_allowed(self):
        p = self._make(MOCK_SVC)
        allowed, _ = p.check("escalate", 0.0, diag("escalate", 0.0))
        self.assertTrue(allowed)

    def test_low_confidence_restart_denied(self):
        p = self._make(MOCK_SVC)
        allowed, reason = p.check("restart", 0.6, diag("restart", 0.6))
        self.assertFalse(allowed)
        self.assertIn("置信度", reason)

    def test_low_confidence_kill_stale_denied(self):
        p = self._make(MOCK_SVC)
        allowed, _ = p.check("kill_stale", 0.5, diag("kill_stale", 0.5))
        self.assertFalse(allowed)

    def test_high_confidence_restart_allowed(self):
        p = self._make(MOCK_SVC)
        allowed, _ = p.check("restart", 0.9, diag("restart", 0.9))
        self.assertTrue(allowed)

    def test_cleanup_logs_allowed(self):
        p = self._make(MOCK_SVC)
        allowed, _ = p.check("cleanup_logs", 0.9, diag("cleanup_logs", 0.9))
        self.assertTrue(allowed)

    def test_rag_qa_only_escalate(self):
        p = self._make(RAG_QA)
        self.assertFalse(p.check("restart", 0.9, diag("restart", 0.9))[0])
        self.assertFalse(p.check("cleanup_logs", 0.9, diag("cleanup_logs", 0.9))[0])
        self.assertTrue(p.check("escalate", 0.5, diag("escalate", 0.5))[0])

    def test_kill_stale_requires_pattern(self):
        p = self._make(NO_PATTERN)
        allowed, reason = p.check("kill_stale", 0.9, diag("kill_stale", 0.9))
        self.assertFalse(allowed)
        self.assertIn("pattern", reason)

    def test_audit_written(self):
        p = self._make(MOCK_SVC)
        p.check("restart", 0.9, diag("restart", 0.9))
        p.check("restart", 0.6, diag("restart", 0.6))
        lines = self.audit.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(json.loads(lines[0])["allowed"])
        self.assertFalse(json.loads(lines[1])["allowed"])


if __name__ == "__main__":
    unittest.main()
