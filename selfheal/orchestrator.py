"""selfheal/orchestrator.py — 编排器主循环。

把「感知→诊断→守门→执行→验证→兜底」串成状态机驱动的完整闭环。

tick(service_name)：单次检查循环（探针 → 状态机事件 → 若进 diagnosing 则
走完 诊断→策略→执行→验证→retry/escalate 全流程 → 记录 Incident）。

灵魂原则：LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器
宣布恢复。诊断选择「置信度高且 policy 允许的」候选；无一允许时走 escalate。
"""

import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import selfheal.executor as executor_module
from selfheal.audit_store import append as audit_append
from selfheal.models import Incident, TickResult
from selfheal.policy import PolicyEngine
from selfheal.probe import HTTPProbe, make_probe
from selfheal.verifier import verify as verify_fn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_LOG = PROJECT_ROOT / "audit" / "incidents.jsonl"

# run_once 判定生命周期终止的状态
TERMINAL_STATES = ("healthy", "awaiting_human", "quarantined")


class Orchestrator:
    def __init__(self, services_config: dict, state_machines: dict,
                 diagnoser_rule, diagnoser_llm=None,
                 policy=None, executor=None, verifier=None,
                 extra_probes=None):
        self.services = services_config
        self.state_machines = state_machines
        self.rule = diagnoser_rule
        self.llm = diagnoser_llm
        self.executor = executor or executor_module
        self.verifier = verifier or verify_fn
        self.extra_probes = list(extra_probes) if extra_probes else []

        # policy 参数兼容三种形态：None（按服务自动建）/ dict（按服务查）/ PolicyEngine 实例
        self.policy_arg = policy
        self._policy_cache: dict = {}

        self._open: dict = {}            # service_name -> Incident（未完结）
        self._last_probe = None
        self._last_diag = None
        self._last_action = None
        self._last_verified = None
        self._policy_gate = (False, "")
        self._has_diagnosis = False

    # ---------- 对外接口 ----------

    def tick(self, service_name: str) -> TickResult:
        sm = self.state_machines[service_name]
        svc = self.services[service_name]

        # 1. 探针
        probe_result = self._probe_service(svc)

        # 2. 状态机事件（repair lock 期间不喂新探针事件）
        if not sm.repair_lock:
            sm.event("probe_ok" if probe_result.ok else "probe_fail")

        # 3-4. 驱动诊断/策略/执行/验证流程
        self._drive(service_name, sm, svc)

        # 5. Incident 在 _drive 的终态处落盘
        return TickResult(service_name, sm.state, probe_result.ok,
                          self._last_diag, self._last_action, self._last_verified)

    def run_once(self, service_name: str, max_ticks: int = 50, sleep_sec: float = 0.1) -> list:
        """跑一个完整生命周期（demo/测试用）。返回 tick 结果列表。

        终止条件：升级人工/隔离（awaiting_human/quarantined），或恢复后健康且探针 ok。
        「healthy 但探针失败」不算终态——故障正在累积（pending_failures）。
        """
        sm = self.state_machines[service_name]
        sm.reset()
        results = []
        for _ in range(max_ticks):
            result = self.tick(service_name)
            results.append(result)
            state = sm.state
            if state in ("awaiting_human", "quarantined"):
                break
            if state == "healthy" and result.probe_ok:
                break
            if state == "recovered":
                wait = max(0.0, sm.cooldown_until - time.time())
                if wait:
                    time.sleep(wait)
                continue
            time.sleep(sleep_sec)
        return results

    def loop(self, interval: float = 5):
        """持续监控（Ctrl+C 停止）。"""
        print("[orchestrator] 开始持续监控 (Ctrl+C 停止)")
        try:
            while True:
                for name in self.services:
                    try:
                        r = self.tick(name)
                        print(f"[{datetime.now():%H:%M:%S}] {name}: "
                              f"state={r.state} probe_ok={r.probe_ok} "
                              f"action={r.action} verified={r.verified}")
                    except Exception as e:
                        print(f"[{name}] tick 异常: {e}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[orchestrator] 已停止")

    # ---------- 驱动核心 ----------

    def _drive(self, service_name: str, sm, svc) -> None:
        state = sm.state

        if state == "diagnosing":
            self._open_incident(service_name)
            diag = self._diagnose(service_name)   # 选「置信度高且 policy 允许」的
            if not self._has_diagnosis:
                sm.event("no_diagnosis")
                self._route_escalation(service_name, sm, svc, "无诊断结果")
                return
            sm.event("diagnosis_ready")            # -> policy_check
            if diag is None:
                # 有诊断但无一被策略允许 → 策略门拒绝 → escalate
                sm.event("denied")
                self._route_escalation(service_name, sm, svc,
                                       f"策略拒绝自动修复（{self._policy_gate[1]}）")
                return
            if diag.suggested_action == "escalate":
                # 诊断建议 escalate：复用 denied 事件 → 直接升级人工
                sm.event("denied")
                self._route_escalation(service_name, sm, svc, f"诊断建议 escalate: {diag.reason}")
                return
            sm.event("allowed")                    # -> repairing
            self._repair_loop(service_name, sm, svc, diag)

        elif state == "recovered":
            if time.time() >= sm.cooldown_until:
                sm.event("cooldown_done")
                if sm.state == "healthy":
                    self._close_incident(service_name, sm, self._last_action, ok=True)

        # 其余状态（healthy/degraded/awaiting_human/quarantined）本轮无事可做

    def _repair_loop(self, service_name: str, sm, svc, diag) -> None:
        action = diag.suggested_action
        for _ in range(10):  # 保险丝：防止意外死循环
            if sm.state == "repairing":
                result = self.executor.execute(action, svc)
                self._last_action = action
                if result.ok:
                    sm.event("started")            # -> starting
                else:
                    sm.event("fail")               # -> retry
                    self._handle_retry(service_name, sm, svc, f"执行 {action} 失败: {result.detail}")
            elif sm.state == "starting":
                v = self._verify_service(svc)      # 确定性验证器
                self._last_verified = v.ok
                if v.ok:
                    sm.event("ready")              # -> verifying
                    sm.event("pass")               # -> recovered
                    return
                sm.event("timeout")                # -> retry
                self._handle_retry(service_name, sm, svc, f"启动后验证失败: {v.detail}")
            else:
                return

    def _handle_retry(self, service_name: str, sm, svc, reason: str) -> None:
        if sm.state != "retry":
            return
        if sm.retry_count >= sm.max_retries:
            sm.event("budget_exhausted")           # -> escalated
            self._route_escalation(service_name, sm, svc,
                                   f"修复预算耗尽（{sm.retry_count}/{sm.max_retries} 次）：{reason}")
        else:
            sm.event("budget_left")                # -> repairing 重试

    def _route_escalation(self, service_name: str, sm, svc, reason: str) -> None:
        self.executor.execute("escalate", svc, reason=reason)   # 写审计
        self._last_action = "escalate"
        if sm.state == "escalated":
            sm.event("human_ack")                  # -> awaiting_human
        self._close_incident(service_name, sm, "escalate", ok=False)

    # ---------- 探针 / 诊断 / 验证 ----------

    def _probe_service(self, svc: dict):
        """主探针 + 附加探针聚合：任一失败 → 整体失败，记录第一个失败探针结果。

        extra_probes 里常驻的 ResourceProbe 负责探测 HTTP 健康端点感知不到的
        资源类故障（cpu_spike/mem_leak）。诊断读 self._last_probe 的 status。
        """
        probe = svc.get("probe") or {}
        ptype = probe.get("type", "http")
        if ptype == "http":
            p = HTTPProbe(probe["url"], timeout=probe.get("timeout", 5))
        else:
            p = make_probe(ptype, **self._probe_kwargs(svc))
        results = [p.check()]
        for extra in self.extra_probes:
            results.append(extra.check())
        for r in results:
            if not r.ok:
                self._last_probe = r
                return r
        self._last_probe = results[0]
        return results[0]

    @staticmethod
    def _probe_kwargs(svc: dict) -> dict:
        probe = svc.get("probe") or {}
        process = svc.get("process") or {}
        kwargs = dict(probe.get("params") or {})
        if probe.get("type") == "process":
            kwargs.setdefault("pid_file", process.get("pid_file"))
            kwargs.setdefault("name", process.get("name") or process.get("pattern"))
            kwargs.setdefault("pattern", process.get("pattern"))
            kwargs.setdefault("expected_count", process.get("expected_count"))
        return kwargs

    def _diagnose(self, service_name: str):
        """收集 Rule + LLM 候选，返回置信度最高且 policy 允许的（None=无一允许）。"""
        candidates = [self.rule.diagnose(self._last_probe)]
        self._has_diagnosis = True
        self._last_diag = candidates[0]            # 报告用：规则结果
        if self.llm is not None:
            llm_diag = self.llm.diagnose(self._last_probe,
                                         log_tail=self._read_log_tail(service_name))
            if llm_diag is not None:
                candidates.append(llm_diag)
                self._last_diag = llm_diag if llm_diag.confidence >= self._last_diag.confidence else self._last_diag

        policy = self._policy_for(service_name)
        best = None
        gate = (False, "无诊断允许")
        inc = self._open.get(service_name)
        inc_id = inc.incident_id if inc else None
        for d in sorted(candidates, key=lambda x: x.confidence, reverse=True):
            allowed, reason = policy.check(d.suggested_action, d.confidence, d,
                                           incident_id=inc_id)
            if allowed:
                best = d
                gate = (True, reason)
                break
        self._policy_gate = gate
        return best

    def _verify_service(self, svc: dict):
        v = svc.get("verifier") or {}
        kind = v.get("type", "http")
        kwargs = dict(v.get("params") or {})
        if kind == "http":
            kwargs.setdefault("url", v["url"])
            kwargs.setdefault("timeout", v.get("timeout", 5.0))
            kwargs.setdefault("retries", v.get("retries", 3))
            kwargs.setdefault("backoff", v.get("backoff", 1.0))
            kwargs.setdefault("samples", v.get("samples", 3))
            kwargs.setdefault("sample_interval", v.get("sample_interval", 0.5))
        elif kind == "process":
            process = svc.get("process") or {}
            kwargs.setdefault("name", process.get("name"))
            kwargs.setdefault("pid_file", process.get("pid_file"))
        elif kind == "db":
            kwargs.setdefault("path", v.get("path"))
        return self.verifier(kind, **kwargs)

    @staticmethod
    def _read_log_tail(service_name: str, max_chars: int = 2000) -> str:
        """mock 环境日志片段（尽力而为，无则空串）。"""
        log = PROJECT_ROOT / "mockenv" / "logs" / f"{service_name}.log"
        try:
            if not log.exists():
                return ""
            return log.read_text(encoding="utf-8", errors="replace")[-max_chars:]
        except OSError:
            return ""

    # ---------- Incident 记录 ----------

    def _open_incident(self, service_name: str) -> None:
        if service_name not in self._open:
            self._open[service_name] = Incident(
                service=service_name,
                incident_id=str(uuid4()),
                started_at=datetime.now().isoformat(timespec="seconds"),
            )

    def _close_incident(self, service_name: str, sm, action, ok: bool) -> None:
        inc = self._open.pop(service_name, None)
        if inc is None:
            return
        inc.state_path = sm.state_path
        inc.action = action
        inc.ok = ok
        inc.ended_at = datetime.now().isoformat(timespec="seconds")
        self._append_incident(inc)

    @staticmethod
    def _append_incident(inc: Incident) -> None:
        entry = asdict(inc)
        entry["schema_version"] = 1
        audit_append(INCIDENTS_LOG, entry)

    # ---------- 策略门 ----------

    def _policy_for(self, service_name: str) -> PolicyEngine:
        p = self.policy_arg
        if p is None or (isinstance(p, dict) and service_name not in p):
            if service_name not in self._policy_cache:
                self._policy_cache[service_name] = PolicyEngine(self.services[service_name])
            return self._policy_cache[service_name]
        if isinstance(p, dict):
            return p[service_name]
        return p  # 单个 PolicyEngine 实例（单服务场景）
