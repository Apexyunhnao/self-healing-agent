"""tests/test_llm_diagnoser.py — LLM 诊断器：无 key 降级 / 坏 JSON 降级 / 合法 JSON 通过。

_call 契约（成本审计引入）：返回 (raw, usage, duration_s)；测试用 lambda 模拟时
必须返回三元组，否则 diagnose 解包失败。
"""

import json
import tempfile
import unittest
from pathlib import Path

from selfheal.llm_diagnoser import CALLS_AUDIT, LLMDiagnoser, load_api_key
from selfheal.probe import ProbeResult

SVC = {"display": "t", "probe": {"type": "http", "url": "http://x/healthz"}}

VALID_JSON = json.dumps({
    "root_cause": "process_down",
    "confidence": 0.9,
    "suggested_action": "restart",
    "reason": "进程挂了",
})


def _mock_call(raw):
    """把旧契约的"返回 raw 字符串"包成新三元组契约。"""
    return lambda prompt: (raw, {"input_tokens": 10, "output_tokens": 5}, 0.42)


class TestLLMDiagnoser(unittest.TestCase):
    def test_no_key_returns_none(self):
        # 显式传空串 key（强制无 key，不读 .env）→ 直接降级 None
        d = LLMDiagnoser(SVC, api_key="")
        self.assertFalse(d.api_key)   # 空 key → 无 key，直接降级
        self.assertIsNone(d.diagnose(ProbeResult(False, "network_error", "x")))

    def test_bad_json_returns_none(self):
        d = LLMDiagnoser(SVC, api_key="test-key")
        d._call = _mock_call("not json at all")
        self.assertIsNone(d.diagnose(ProbeResult(False, "network_error", "x")))

    def test_missing_fields_returns_none(self):
        d = LLMDiagnoser(SVC, api_key="test-key")
        d._call = _mock_call(json.dumps({"root_cause": "x"}))
        self.assertIsNone(d.diagnose(ProbeResult(False, "network_error", "x")))

    def test_valid_json_parsed(self):
        d = LLMDiagnoser(SVC, api_key="test-key")
        d._call = _mock_call(VALID_JSON)
        r = d.diagnose(ProbeResult(False, "network_error", "x"))
        self.assertIsNotNone(r)
        self.assertEqual(r.root_cause, "process_down")
        self.assertEqual(r.suggested_action, "restart")
        self.assertEqual(r.confidence, 0.9)
        self.assertEqual(r.diagnoser, "llm")

    def test_action_not_in_whitelist_returns_none(self):
        d = LLMDiagnoser(SVC, api_key="test-key")
        d._call = _mock_call(json.dumps({
            "root_cause": "x", "confidence": 0.9,
            "suggested_action": "rm_rf", "reason": "bad",
        }))
        self.assertIsNone(d.diagnose(ProbeResult(False, "network_error", "x")))

    def test_confidence_out_of_range_returns_none(self):
        d = LLMDiagnoser(SVC, api_key="test-key")
        d._call = _mock_call(json.dumps({
            "root_cause": "x", "confidence": 1.5,
            "suggested_action": "restart", "reason": "bad",
        }))
        self.assertIsNone(d.diagnose(ProbeResult(False, "network_error", "x")))

    def test_call_count_increments(self):
        d = LLMDiagnoser(SVC, api_key="test-key")
        d._call = _mock_call(VALID_JSON)
        d.diagnose(ProbeResult(False, "network_error", "x"))
        d.diagnose(ProbeResult(False, "network_error", "x"))
        self.assertEqual(d.call_count, 2)

    def test_audit_call_writes_costs_jsonl(self):
        d = LLMDiagnoser(SVC, api_key="test-key")
        with tempfile.TemporaryDirectory() as tmp:
            from selfheal import llm_diagnoser
            orig = llm_diagnoser.CALLS_AUDIT
            llm_diagnoser.CALLS_AUDIT = Path(tmp) / "llm_calls.jsonl"
            try:
                d._call = _mock_call(VALID_JSON)
                d.diagnose(ProbeResult(False, "network_error", "x"))
                lines = llm_diagnoser.CALLS_AUDIT.read_text(encoding="utf-8").splitlines()
            finally:
                llm_diagnoser.CALLS_AUDIT = orig
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["input_tokens"], 10)
            self.assertEqual(entry["output_tokens"], 5)
            self.assertEqual(entry["duration_s"], 0.42)
            self.assertIn("ts", entry)
            self.assertIn("model", entry)

    def test_load_api_key_empty_env_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
            self.assertIsNone(load_api_key(env))


if __name__ == "__main__":
    unittest.main()
