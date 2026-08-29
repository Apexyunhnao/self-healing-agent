#!/usr/bin/env python3
"""mockenv/verify_env.py — mock 环境自动验证

检查项：
  A. 启动 mock_server --daemon → PID 文件存在 + /healthz 200
  B. 注入 proc_crash_01 (kill_pid) → 进程已死 → recover → 再启动成功
  C. 注入 http_500_06 (http_5xx) → /healthz 500 → recover → /healthz 200
  D. 注入 db_lock_12 (db_lock) → DB 被锁（打开失败）→ recover → 可正常打开
  E. fault_injector list → 35 场景（单一 24 / mixed 5 / baseline 6）

用法: python verify_env.py
"""

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = sys.executable
RUN_DIR = BASE / "run"
STATE_DIR = BASE / "state"
DB_FILE = STATE_DIR / "mock_svc.db"
SERVICE = "mock_svc"

results = []  # (name, ok, detail)


def report(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def run_script(script: str, args=(), timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(BASE / script), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def free_port() -> int:
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if 10000 <= p <= 60000:
            return p


def is_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10)
        return f'"{pid}"' in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=15)
    else:
        os.kill(pid, signal.SIGKILL)


def http_get(port: int, path: str = "/healthz", timeout: float = 8.0):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        import httpx
        # trust_env=False：不走 Windows 系统代理（V2RayN 代理会拒绝转发 localhost → 503）
        r = httpx.get(url, timeout=timeout, trust_env=False)
        return r.status_code, r.text
    except ImportError:
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            return -1, str(e)
    except Exception as e:
        return -1, str(e)


def start_mock(port: int) -> None:
    """以 daemon 方式启动 mock 并清理旧 PID 文件（避免读到僵尸文件）。"""
    (RUN_DIR / f"{SERVICE}.pid").unlink(missing_ok=True)
    run_script("mock_server.py", ["--port", str(port), "--name", SERVICE, "--daemon"])


def wait_mock(port: int, timeout: float = 15.0) -> bool:
    """等 PID 文件出现、进程存活且 /healthz 200。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid_file = RUN_DIR / f"{SERVICE}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_alive(pid):
                code, body = http_get(port)
                if code == 200 and '"status": "ok"' in body:
                    return True
        time.sleep(0.5)
    return False


def cleanup() -> None:
    """清掉所有残留注入 + 残留 mock 进程 + 状态文件。"""
    for marker in sorted(STATE_DIR.glob("injected_*.json")):
        sid = marker.name[len("injected_"):-len(".json")]
        run_script("fault_injector.py", ["recover", sid])
    # 杀所有命令行含 mock_server.py 的残留进程（不依赖 pid 文件，防止调试残留串台）
    if os.name == "nt":
        subprocess.run(["wmic", "process", "where", "CommandLine like '%mock_server.py%'",
                        "call", "terminate"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=15)
        time.sleep(0.5)
    for pid_file in RUN_DIR.glob("*.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            continue
        if is_alive(pid):
            kill_pid(pid)
        pid_file.unlink(missing_ok=True)
    # 删服务状态文件（防止残留故障模式影响下次启动）
    for f in list(STATE_DIR.glob("mock_svc.json")) + list(STATE_DIR.glob("mock_db.json")):
        f.unlink(missing_ok=True)


def db_openable(timeout: float = 2.0) -> bool:
    try:
        with sqlite3.connect(DB_FILE, timeout=timeout) as conn:
            conn.execute("SELECT 1")
        return True
    except sqlite3.Error:
        return False


def main() -> None:
    print("== mock 环境验证 ==")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    cleanup()

    port = free_port()

    # A. 启动 + 健康检查
    start_mock(port)
    ok_a = wait_mock(port)
    pid_file = RUN_DIR / f"{SERVICE}.pid"
    report("A 启动 mock(daemon) + /healthz 200", ok_a,
           f"port={port}, pid 文件={'存在' if pid_file.exists() else '缺失'}")

    # B. proc_crash_01
    pid_before = int(pid_file.read_text().strip()) if pid_file.exists() else 0
    run_script("fault_injector.py", ["inject", "proc_crash_01"])
    time.sleep(1.0)
    dead = not is_alive(pid_before)
    report("B1 注入 proc_crash_01 → 进程已死", dead, f"pid={pid_before}")

    run_script("fault_injector.py", ["recover", "proc_crash_01"])
    start_mock(port)
    ok_b2 = wait_mock(port)
    report("B2 recover 后重启成功", ok_b2, f"port={port} /healthz 200")
    if ok_b2:
        pid_before = int(pid_file.read_text().strip())

    # C. http_500_06
    run_script("fault_injector.py", ["inject", "http_500_06"])
    code, body = http_get(port)
    report("C1 注入 http_500_06 → /healthz 500", code == 500, f"code={code} body={body.strip()[:60]}")

    run_script("fault_injector.py", ["recover", "http_500_06"])
    code, body = http_get(port)
    report("C2 recover → /healthz 200", code == 200 and '"status": "ok"' in body,
           f"code={code} body={body.strip()[:60]}")

    # D. db_lock_12
    run_script("fault_injector.py", ["inject", "db_lock_12"])
    locked = not db_openable(timeout=2.0)
    report("D1 注入 db_lock_12 → DB 被锁（打开失败）", locked,
           "sqlite SELECT 失败(database is locked)" if locked else "DB 仍可打开")

    run_script("fault_injector.py", ["recover", "db_lock_12"])
    ok_d2 = db_openable(timeout=5.0)
    report("D2 recover → DB 可正常打开", ok_d2, "sqlite SELECT 成功" if ok_d2 else "仍被锁")

    # E. 场景清单
    out = run_script("fault_injector.py", ["list"]).stdout
    rows = [l for l in out.splitlines() if l.count("  ") >= 2 and "场景" not in l]
    rows = [l for l in rows if "single" in l or "MIXED" in l or "baseline" in l]
    n = len(rows)
    n_mixed = sum(1 for l in rows if "MIXED" in l)
    n_baseline = sum(1 for l in rows if "baseline" in l)
    matrix = json.loads((BASE / "scenarios" / "fault_matrix.json").read_text(encoding="utf-8"))
    n_json = len(matrix["scenarios"])
    report("E 场景清单 35（单一24/mixed5/baseline6）",
           n == 35 and n_mixed == 5 and n_baseline == 6 and n_json == 35,
           f"list 输出 {n} 条 (mixed {n_mixed}, baseline {n_baseline}), JSON 解析 {n_json} 个")

    # 收尾：杀掉 mock，清状态
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if is_alive(pid):
                kill_pid(pid)
        except ValueError:
            pass
        pid_file.unlink(missing_ok=True)
    (RUN_DIR / f"{SERVICE}.port").unlink(missing_ok=True)
    (STATE_DIR / f"{SERVICE}.json").unlink(missing_ok=True)
    (STATE_DIR / f"disk_fill_{SERVICE}.bin").unlink(missing_ok=True)

    # 报告
    print("\n========== mock 环境验证报告 ==========")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    n_pass = sum(1 for _, ok, _ in results if ok)
    print(f"通过 {n_pass}/{len(results)}")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
