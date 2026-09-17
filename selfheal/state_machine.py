"""selfheal/state_machine.py — 手写状态机（确定性核心，禁止 import LLM）。

状态：healthy / degraded / diagnosing / policy_check / repairing / starting /
      verifying / recovered / rolling_back / retry / escalated / awaiting_human /
      quarantined
转移规则写死为常量表 TRANSITIONS；核心转移函数 transition(state, event) 是纯函数。
计数类转移（连续探针失败、修复预算、冷却、quarantine）由 StateMachine 类包装处理。
"""

from time import time

# ---- 常量表：状态 / 事件 / 转移 ----

STATES = frozenset({
    "healthy", "degraded", "diagnosing", "policy_check", "repairing",
    "starting", "verifying", "recovered", "rolling_back", "retry",
    "escalated", "awaiting_human", "quarantined",
})

# 修复中状态：该服务 repair lock 生效，拒绝新事件
REPAIR_LOCK_STATES = frozenset({"repairing", "starting", "verifying"})

# 连续探针失败阈值：healthy 连续 2 次失败 → degraded；第 3 次 → diagnosing
PROBE_FAIL_THRESHOLD = 2
DIAGNOSE_THRESHOLD = 3

# 修复预算：同一故障 3 次修复失败 → 强制升级人工
MAX_RETRIES = 3

# recovered 后冷却秒数
COOLDOWN_SECONDS = 30

# 同一服务连续 2 次 escalated → quarantine
ESCALATE_LIMIT = 2

# 纯函数转移表：(state, event) -> next_state
TRANSITIONS = {
    ("healthy", "probe_ok"): "healthy",
    ("healthy", "probe_fail"): "degraded",
    ("degraded", "probe_ok"): "healthy",
    ("degraded", "probe_fail"): "diagnosing",
    ("diagnosing", "diagnosis_ready"): "policy_check",
    ("diagnosing", "no_diagnosis"): "escalated",
    ("policy_check", "allowed"): "repairing",
    ("policy_check", "denied"): "escalated",
    ("repairing", "started"): "starting",
    ("repairing", "fail"): "retry",
    ("starting", "ready"): "verifying",
    ("starting", "timeout"): "retry",
    ("verifying", "pass"): "recovered",
    ("verifying", "fail"): "retry",
    ("recovered", "cooldown_done"): "healthy",
    ("retry", "budget_left"): "repairing",
    ("retry", "budget_exhausted"): "escalated",
    ("escalated", "human_ack"): "awaiting_human",
    ("awaiting_human", "service_fixed"): "healthy",
}


def transition(state: str, event: str) -> str:
    """纯函数：返回 (state, event) 的下一个状态。非法转移抛 ValueError。

    特殊规则：任何状态 --quarantine--> quarantined。
    """
    if event == "quarantine":
        if state == "quarantined":
            raise ValueError(f"已在 quarantined，无需再转移: {state} --{event}->")
        return "quarantined"
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise ValueError(f"非法状态转移: {state} --{event}-> ?")


class StateMachine:
    """单服务状态机。

    计数语义：
        - pending_failures：连续探针失败计数。healthy 下第 1 次失败只记录，
          第 2 次连续失败 → degraded；degraded 下第 3 次连续失败 → diagnosing。
        - retry_count：进入 retry 状态累计。达到 MAX_RETRIES → budget_exhausted → escalated。
        - escalation_count：进入 escalated 累计。达到 ESCALATE_LIMIT → 自动 quarantine。
        - cooldown_until：recovered 后冷却时间戳，冷却结束才允许 cooldown_done → healthy。
        - repair_lock：repairing/starting/verifying 期间为 True，编排器拒绝喂新事件。
    """

    def __init__(self, service: str, cooldown_seconds: int = COOLDOWN_SECONDS,
                 max_retries: int = MAX_RETRIES):
        self.service = service
        self.cooldown_seconds = cooldown_seconds
        self.max_retries = max_retries
        self._state = "healthy"
        self.pending_failures = 0
        self.retry_count = 0
        self.escalation_count = 0
        self.cooldown_until = 0.0
        self._history: list = ["healthy"]  # 状态路径以 healthy 为起点

    # ---- 只读属性 ----

    @property
    def state(self) -> str:
        return self._state

    @property
    def state_path(self) -> list:
        return list(self._history)

    @property
    def repair_lock(self) -> bool:
        return self._state in REPAIR_LOCK_STATES

    # ---- 事件驱动 ----

    def event(self, name: str) -> str:
        """驱动一个事件；返回新状态。非法转移抛 ValueError。"""
        s = self._state

        if name == "probe_ok":
            self.pending_failures = 0
            if s == "degraded":
                self._enter("healthy")   # healthy 上再 probe_ok 是 no-op，不重复记录
            return self._state

        if name == "probe_fail":
            # 只对 healthy/degraded 计数；其余状态（修复中/恢复中等）忽略探针失败。
            # 连续失败数跨 degraded 累计：第 2 次失败进 degraded（保留计数），
            # 第 3 次连续失败才进 diagnosing。
            if s == "healthy":
                self.pending_failures += 1
                if self.pending_failures >= PROBE_FAIL_THRESHOLD:
                    self._enter("degraded")
            elif s == "degraded":
                self.pending_failures += 1
                if self.pending_failures >= DIAGNOSE_THRESHOLD:
                    self.pending_failures = 0
                    self._enter("diagnosing")
            return self._state

        if name in ("fail", "timeout"):
            # repairing --fail--> retry；starting --timeout--> retry
            self.retry_count += 1
            self._enter(transition(s, name))
            return self._state

        if name == "cooldown_done":
            if s == "recovered" and time() >= self.cooldown_until:
                self._enter("healthy")
            return self._state

        # 其余事件直接查纯函数表（含 budget_left/budget_exhausted/human_ack/…）
        nxt = transition(s, name)
        self._enter(nxt)
        return self._state

    # ---- 内部 ----

    def _enter(self, state: str) -> None:
        """进入新状态：记录历史 + 副作用（冷却时间戳 / escalate 计数→quarantine）。"""
        self._state = state
        self._history.append(state)
        if state == "recovered":
            self.cooldown_until = time() + self.cooldown_seconds
        if state == "escalated":
            self.escalation_count += 1
            if self.escalation_count >= ESCALATE_LIMIT:
                self._state = "quarantined"
                self._history.append("quarantined")

    def reset(self) -> None:
        """重置到 healthy（编排器 run_once 每次生命周期前调用）。"""
        self._state = "healthy"
        self.pending_failures = 0
        self.retry_count = 0
        self.escalation_count = 0
        self.cooldown_until = 0.0
        self._history = ["healthy"]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StateMachine {self.service}: {self._state}>"
