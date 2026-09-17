"""selfheal/audit_store.py — 审计日志存储（确定性核心，不 import LLM）。

统一 JSONL 写入/读取：
- append(): 追加一行 + size-based 轮转（默认 5MB，保留 3 个备份 .1/.2/.3）
- read(): 读取全部，损坏行跳过并计数（不拖垮整个读取）
- 单条写失败不影响主流程（审计是 side-channel，失败只 warning）
"""

import json
from datetime import datetime
from pathlib import Path

DEFAULT_ROTATE_BYTES = 5 * 1024 * 1024   # 5MB
DEFAULT_KEEP = 3                          # 保留 .1 .2 .3


def append(path: Path, entry: dict, rotate_bytes: int = DEFAULT_ROTATE_BYTES,
           keep: int = DEFAULT_KEEP) -> None:
    """追加一条 JSONL 记录；文件超过 rotate_bytes 时轮转（.1 覆盖 .2 ...）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= rotate_bytes:
            _rotate(path, keep)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:  # 审计失败不影响主流程
        pass


def _rotate(path: Path, keep: int) -> None:
    for i in range(keep, 0, -1):
        src = Path(f"{path}.{i - 1}") if i > 1 else path
        dst = Path(f"{path}.{i}")
        try:
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.replace(dst)
        except OSError:
            pass


def read(path: Path) -> list:
    """读取全部记录；缺失→[]，损坏行跳过（计数放 meta）。"""
    rows = []
    skipped = 0
    if not path.exists():
        return rows
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
    except OSError:
        return rows
    if skipped:
        rows.append({"_meta": {"skipped_malformed": skipped}})
    return rows


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
