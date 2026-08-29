#!/usr/bin/env python3
"""mockenv/mock_db.py — SQLite mock DB

模拟 DB 故障：--lock 加独占锁并一直持有（进程不退出 = 文件不释放），--corrupt 写坏文件。
Windows 下用 sqlite 事务独占锁实现（等价于 fcntl 打开不释放的跨平台方案）。

用法:
    python mock_db.py --init       # 初始化 kv 表（默认行为）
    python mock_db.py --lock       # 加独占锁并保持（供注入器以子进程拉起）
    python mock_db.py --corrupt    # 写坏 DB 文件（模拟 integrity failure）
    python mock_db.py --recreate   # 删掉重建（recover 用）
    python mock_db.py --check      # 检查 DB 能否打开（exit 0/1）
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "state" / "mock_svc.db"


def connect(timeout: float = 5.0) -> sqlite3.Connection:
    return sqlite3.connect(DB, timeout=timeout)


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR IGNORE INTO kv VALUES ('service', 'mock')")
        conn.commit()
    print(f"[mock_db] initialized {DB}")


def lock_forever() -> None:
    """对 DB 加独占锁并一直持有（BEGIN EXCLUSIVE，进程不退出的等价物）。"""
    init_db()
    conn = connect(timeout=0.5)
    conn.execute("BEGIN EXCLUSIVE")
    print(f"[mock_db] locked (pid {os.getpid()})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def corrupt() -> None:
    """向 DB 文件写入损坏字节（覆盖头部 + 追加垃圾），使 SQLite 无法打开。"""
    init_db()
    with DB.open("r+b") as f:
        f.seek(0)
        f.write(b"\x00" * 64)
        f.write(b"MOCK_CORRUPTED_" * 32)
        f.seek(0, 2)
        f.write(b"\xff\xfe garbage" * 64)
    print(f"[mock_db] corrupted {DB}")


def recreate() -> None:
    if DB.exists():
        DB.unlink()
    init_db()
    print(f"[mock_db] recreated {DB}")


def check() -> bool:
    if not DB.exists():
        return False
    try:
        with connect(timeout=2.0) as conn:
            conn.execute("SELECT 1")
        return True
    except sqlite3.Error:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite mock DB")
    parser.add_argument("--init", action="store_true", help="初始化 kv 表")
    parser.add_argument("--lock", action="store_true", help="加独占锁并保持")
    parser.add_argument("--corrupt", action="store_true", help="写坏 DB 文件")
    parser.add_argument("--recreate", action="store_true", help="删除并重建 DB")
    parser.add_argument("--check", action="store_true", help="检查 DB 可打开性（exit 0/1）")
    args = parser.parse_args()

    if args.lock:
        lock_forever()
    elif args.corrupt:
        corrupt()
    elif args.recreate:
        recreate()
    elif args.check:
        sys.exit(0 if check() else 1)
    else:
        init_db()


if __name__ == "__main__":
    main()
