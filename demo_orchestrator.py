#!/usr/bin/env python3
"""demo_orchestrator.py — mock 环境演示完整生命周期。

三条路径（阶段 3 编排层验收演示）：
  1. proc_crash_01  进程崩溃（真实故障）→ 规则诊断 process_down@0.9 → 策略放行
     restart → 验证通过 → recovered → healthy（自动自愈）
  2. http_500_06    状态型故障 → 规则建议 restart@0.6 → 策略门拒绝（置信度 < 0.7）
     → escalated → awaiting_human（LLM/规则可以提出，但只有策略门允许执行）
  3. proc_crash + http_500 残留：规则给 process_down@0.9（放行 restart），但
     restart 后验证仍失败 → retry 3 次 → 预算耗尽 → escalated（强制升级人工）

无 DEEPSEEK_API_KEY 时 LLM 诊断自动降级 Rule（演示中会打印降级提示）。

用法: python demo_orchestrator.py
环境: MOCK_PORT 默认 18080。
"""

import os
import subprocess
import sys
from pathlib import Path

from selfheal.config import load_services
from selfheal.executor import execute, kill_stale
from selfheal.llm_diagnoser import LLMDiagnoser
from selfheal.orchestrator import Orchestrator
from selfheal.policy import PolicyEngine
from selfheal.rule_diagnoser import RuleDiagnoser
from selfheal.state_machine import StateMachine
from selfheal.verifier import verify_http

BASE = Path(__file__).resolve().parent
MOCK_PORT = int(os.environ.get("MOCK_PORT", "18080"))
SVC_NAME = "mock_svc"
PY = sys.executable


def run_fault_injector(args) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(BASE / "mockenv" / "fault_injector.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def cleanup() -> None:
    run_fault_injector(["recover", "--all"])
    kill_stale({"display": SVC_NAME, "process": {"pattern": "mock_server.py"}})
    (BASE / "mockenv" / "run" / f"{SVC_NAME}.pid").unlink(missing_ok=True)
    (BASE / "mockenv" / "run" / f"{SVC_NAME}.port").unlink(missing_ok=True)
    (BASE / "mockenv" / "state" / f"{SVC_NAME}.json").unlink(missing_ok=True)


def start_service(svc: dict) -> None:
    r = execute("restart", svc)
    assert r.ok, r.detail
    v = verify_http(svc["verifier"]["url"], timeout=5, retries=5, backoff=0.3)
    assert v.ok, v.detail
    print(f"[start] mock_svc 就绪 {svc['probe']['url']}")


def make_orchestrator(svc: dict) -> Orchestrator:
    sm = StateMachine(SVC_NAME, cooldown_seconds=1)
    llm = LLMDiagnoser(svc)   # 读 .env；无 key → 每次诊断打印降级提示并返回 None
    return Orchestrator({SVC_NAME: svc}, {SVC_NAME: sm},
                        diagnoser_rule=RuleDiagnoser(svc),
                        diagnoser_llm=llm,
                        policy=PolicyEngine(svc))


def show_path(sm) -> None:
    print(f"[状态路径] {' → '.join(sm.state_path)}")


def lifecycle_proc_crash(svc: dict) -> None:
    cleanup()
    print("\n" + "=" * 72)
    print("生命周期 1：proc_crash_01 进程被杀 — 规则诊断 process_down → restart → 自愈")
    print("=" * 72)
    start_service(svc)
    cp = run_fault_injector(["inject", "proc_crash_01"])
    print(cp.stdout.strip())

    orch = make_orchestrator(svc)
    results = orch.run_once(SVC_NAME)
    sm = orch.state_machines[SVC_NAME]
    show_path(sm)
    print(f"[结果] state={sm.state} action={results[-1].action} verified={results[-1].verified}")
    assert sm.state == "healthy", "proc_crash_01 应自愈到 healthy"


def lifecycle_http_500(svc: dict) -> None:
    cleanup()
    print("\n" + "=" * 72)
    print("生命周期 2：http_500_06 状态型故障 — 策略门拒绝低置信度自动重启")
    print("=" * 72)
    start_service(svc)
    cp = run_fault_injector(["inject", "http_500_06"])
    print(cp.stdout.strip())

    orch = make_orchestrator(svc)
    results = orch.run_once(SVC_NAME)
    sm = orch.state_machines[SVC_NAME]
    diag = results[-1].diagnosis
    show_path(sm)
    if diag:
        print(f"[规则诊断] root_cause={diag.root_cause} action={diag.suggested_action} "
              f"confidence={diag.confidence} —— 置信度 {diag.confidence:.1f} < 0.7，策略门拒绝自动 restart")
    print(f"[结果] state={sm.state}（升级人工，等待人工处理）")
    assert sm.state in ("awaiting_human", "quarantined"), "http_500_06 应升级人工"


def lifecycle_budget_exhaust(svc: dict) -> None:
    cleanup()
    print("\n" + "=" * 72)
    print("生命周期 3：proc_crash + http_500 残留 — restart 无效，预算耗尽升级人工")
    print("=" * 72)
    start_service(svc)
    cp = run_fault_injector(["inject", "proc_crash_01"])
    print(cp.stdout.strip())
    # 进程被杀（rule 给 process_down@0.9 → 放行 restart）+ 状态残留 http_500（restart 无效）
    (BASE / "mockenv" / "state" / f"{SVC_NAME}.json").write_text(
        '{"modes": ["http_500"]}', encoding="utf-8")

    orch = make_orchestrator(svc)
    results = orch.run_once(SVC_NAME)
    sm = orch.state_machines[SVC_NAME]
    show_path(sm)
    print(f"[结果] state={sm.state} retry_count={sm.retry_count}（预算 {sm.max_retries} 次耗尽 → 升级人工）")
    assert sm.state in ("awaiting_human", "quarantined"), "预算耗尽应升级人工"


def main() -> int:
    services = load_services(BASE / "config" / "service.yaml",
                             env={"MOCK_PORT": str(MOCK_PORT)})
    svc = services[SVC_NAME]
    v = svc.setdefault("verifier", {})
    v["backoff"] = 0.2          # 演示加速：缩短验证退避
    v["retries"] = 2

    try:
        lifecycle_proc_crash(svc)
        lifecycle_http_500(svc)
        lifecycle_budget_exhaust(svc)
    finally:
        print("\n" + "=" * 72)
        print("收尾清理")
        print("=" * 72)
        cleanup()
        print("残留注入已清、mock 进程已杀、状态文件已清")
    print("\nDemo 完成 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
