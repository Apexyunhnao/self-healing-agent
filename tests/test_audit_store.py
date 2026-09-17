"""tests/test_audit_store.py — 审计存储：追加、轮转、损坏行处理。"""

import json
import tempfile
import unittest
from pathlib import Path

from selfheal.audit_store import append, read


class TestAuditStore(unittest.TestCase):
    def test_append_and_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "audit.jsonl"
            append(p, {"a": 1})
            append(p, {"b": 2})
            rows = read(p)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["a"], 1)

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read(Path(td) / "nope.jsonl"), [])

    def test_corrupt_line_skipped_with_meta(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "audit.jsonl"
            p.write_text('{"ok": true}\nNOT-JSON\n{"ok": false}\n', encoding="utf-8")
            rows = read(p)
            # 正常行 2 条 + 1 条 _meta 标记
            ok_rows = [r for r in rows if "ok" in r]
            self.assertEqual(len(ok_rows), 2)
            metas = [r for r in rows if "_meta" in r]
            self.assertEqual(len(metas), 1)
            self.assertEqual(metas[0]["_meta"]["skipped_malformed"], 1)

    def test_rotation_keeps_backups(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "audit.jsonl"
            # rotate_bytes=100 → 每次 append ~30 字节，3 次后触发轮转
            for i in range(6):
                append(p, {"i": i, "pad": "x" * 40}, rotate_bytes=100, keep=2)
            self.assertTrue(p.exists())
            self.assertTrue(Path(f"{p}.1").exists())
            # 保留 2 个备份，.3 不应存在
            self.assertFalse(Path(f"{p}.3").exists())

    def test_rotation_data_not_lost(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "audit.jsonl"
            for i in range(5):
                append(p, {"i": i, "pad": "x" * 40}, rotate_bytes=100, keep=2)
            # 当前文件 + .1 + .2 都有数据
            total = len(read(p)) + len(read(Path(f"{p}.1"))) + len(read(Path(f"{p}.2")))
            self.assertGreaterEqual(total, 5)


if __name__ == "__main__":
    unittest.main()
