"""tests/test_rule_diagnoser.py — 规则诊断器：各故障类型 → 根因/动作/置信度。"""

import os
import tempfile
import unittest
from pathlib import Path

from selfheal.probe import ProbeResult
from selfheal.rule_diagnoser import RuleDiagnoser


class TestRuleDiagnoser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)
        # 活进程 PID 文件（本测试进程自身）+ 不存在文件的 PID 文件
        self.live_pid = self.tmp_dir / "live.pid"
        self.live_pid.write_text(str(os.getpid()), encoding="utf-8")
        self.dead_pid = self.tmp_dir / "dead.pid"   # 不存在

    def test_process_down(self):
        svc = {"probe": {"type": "http"}, "process": {"pid_file": str(self.dead_pid)}}
        d = RuleDiagnoser(svc).diagnose(ProbeResult(False, "network_error", "refused"))
        self.assertEqual(d.root_cause, "process_down")
        self.assertEqual(d.suggested_action, "restart")
        self.assertEqual(d.confidence, 0.9)
        self.assertEqual(d.diagnoser, "rule")

    def test_unhealthy_but_alive(self):
        svc = {"probe": {"type": "http"}, "process": {"pid_file": str(self.live_pid)}}
        d = RuleDiagnoser(svc).diagnose(ProbeResult(False, "network_error", "refused"))
        self.assertEqual(d.root_cause, "unhealthy_but_alive")
        self.assertEqual(d.suggested_action, "restart")
        self.assertEqual(d.confidence, 0.5)

    def test_http_500(self):
        d = RuleDiagnoser({"probe": {"type": "http"}}).diagnose(
            ProbeResult(False, "http_500", "HTTP 500"))
        self.assertEqual(d.root_cause, "http_error")
        self.assertEqual(d.suggested_action, "restart")
        self.assertEqual(d.confidence, 0.8)

    def test_http_503(self):
        d = RuleDiagnoser({"probe": {"type": "http"}}).diagnose(
            ProbeResult(False, "http_503", "HTTP 503"))
        self.assertEqual(d.root_cause, "http_error")
        self.assertEqual(d.confidence, 0.8)

    def test_db_corrupt(self):
        d = RuleDiagnoser({"probe": {"type": "db"}}).diagnose(
            ProbeResult(False, "db_integrity", "integrity='not ok'"))
        self.assertEqual(d.root_cause, "db_corrupt")
        self.assertEqual(d.suggested_action, "escalate")
        self.assertEqual(d.confidence, 0.8)

    def test_disk_full(self):
        d = RuleDiagnoser({"probe": {"type": "disk"}}).diagnose(
            ProbeResult(False, "disk_over_threshold", "used=90%"))
        self.assertEqual(d.root_cause, "disk_full")
        self.assertEqual(d.suggested_action, "cleanup_logs")
        self.assertEqual(d.confidence, 0.9)

    def test_unknown_fallback(self):
        d = RuleDiagnoser({"probe": {"type": "http"}}).diagnose(
            ProbeResult(False, "http_404", "HTTP 404"))
        self.assertEqual(d.root_cause, "unknown")
        self.assertEqual(d.suggested_action, "escalate")
        self.assertEqual(d.confidence, 0.3)


if __name__ == "__main__":
    unittest.main()
