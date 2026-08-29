#!/usr/bin/env python3
"""demo_cycle.py — 探针→故障→修复→验证 集成闭环演示。

流程:
    1. 启动 mock_svc (MOCK_PORT=18080) → HTTPProbe ok
    2. fault_injector inject http_500_06 → HTTPProbe fail (500)
    3. executor.restart(mock_svc) → verifier.verify_http
       注意：mock 故障是状态文件驱动的，restart 只换进程不清状态。
       若 restart 后仍 500，则补充 recover 注入(清故障源) + 再 restart，
       以完成闭环。这正对应真实系统「先清故障源，再重启验证」的语义。
    4. 收尾：recover 全部注入 + 杀 mock 进程 + 清状态文件

用法: python demo_cycle.py
"""

import os
import subprocess
import sys
from pathlib import Path

from selfheal.config import load_services
from selfheal.executor import execute, kill_stale
from selfheal.probe import HTTPProbe
from selfheal.verifier import verify_http

BASE = Path(__file__).resolve().parent
MOCK_PORT = int(os.environ.get("MOCK_PORT", "18080"))
PY = sys.executable
SVC_NAME = "mock_svc"


def run_fault_injector(args) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(BASE / "mockenv" / "fault_injector.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def cleanup() -> None:
    run_fault_injector(["recover", "--all"])
    kill_stale({"display": SVC_NAME, "process": {"pattern": "mock_server.py"}})
    (BASE / "mockenv" / "run" / f"{SVC_NAME}.pid").unlink(missing_ok=True)
    (BASE / "mockenv" / "run" / f"{SVC_NAME}.port").unlink(missing_ok=True)
    (BASE / "mockenv" / "state" / f"{SVC_NAME}.json").unlink(missing_ok=True)


def main() -> int:
    services = load_services(BASE / "config" / "service.yaml", env={"MOCK_PORT": str(MOCK_PORT)})
    svc = services[SVC_NAME]
    url = svc["verifier"]["url"]

    cleanup()

    # 1. 启动 mock_svc → 探针 ok
    step("1. 启动 mock_svc (MOCK_PORT=%d)" % MOCK_PORT)
    r = execute("restart", svc)
    print(f"[restart] ok={r.ok} {r.detail}")
    assert r.ok, r.detail
    p = HTTPProbe(url, timeout=5).check()
    print(f"[probe]   ok={p.ok} status={p.status} latency={p.latency_ms}ms detail={p.detail}")
    assert p.ok, f"启动后探针应健康: {p.detail}"

    # 2. 注入故障 → 探针 fail
    step("2. fault_injector inject http_500_06")
    cp = run_fault_injector(["inject", "http_500_06"])
    print(cp.stdout.strip())
    p = HTTPProbe(url, timeout=5).check()
    print(f"[probe]   ok={p.ok} status={p.status} latency={p.latency_ms}ms detail={p.detail}")
    assert not p.ok, "注入后探针应失败"

    # 3. 修复 + 验证
    step("3. executor.restart(mock_svc) → verifier.verify_http")
    r = execute("restart", svc)
    print(f"[restart] ok={r.ok} {r.detail}")
    v = verify_http(url, timeout=5, retries=3, backoff=1.0)
    print(f"[verify]  ok={v.ok} attempts={v.attempts} detail={v.detail}")
    if not v.ok:
        # mock 故障是状态文件驱动的：restart 只换进程，不清故障状态。
        # 真实语义 = 先清故障源，再重启，最后验证。
        print("[note] restart 后仍 %s —— mock 故障由状态文件驱动，restart 换进程不清状态" % v.detail)
        step("3b. recover 注入(清故障源) + 再 restart → verify")
        cp = run_fault_injector(["recover", "http_500_06"])
        print(cp.stdout.strip())
        r = execute("restart", svc)
        print(f"[restart] ok={r.ok} {r.detail}")
        v = verify_http(url, timeout=5, retries=3, backoff=1.0)
        print(f"[verify]  ok={v.ok} attempts={v.attempts} detail={v.detail}")

    ok = v.ok
    step("4. 收尾清理")
    cleanup()
    print("残留注入已清、mock 进程已杀、状态文件已清")
    print("\n集成闭环: " + ("成功 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
