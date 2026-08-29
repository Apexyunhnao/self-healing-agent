"""selfheal/policy.py — 策略门（确定性核心，禁止 import LLM）。

LLM/Rule 只能提出动作，只有策略门允许才执行。每次 check 结果追加到 audit/policy.jsonl。

规则：
    - action 必须在 service.yaml 的 repair.actions 白名单内
    - escalate 永远允许
    - confidence < 0.7 且 action 是 restart/kill_stale → 拒绝（走 escalate）
    - kill_stale 必须带明确进程 pattern（service.yaml process.pattern），否则拒绝
    - rag_qa 等真实服务 repair.actions 只有 [escalate]，白名单天然拦截其余动作
"""

import json
from datetime import datetime
from pathlib import Path

from selfheal.audit_store import append as audit_append
from selfheal.models import DiagnosisResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POLICY_AUDIT = PROJECT_ROOT / "audit" / "policy.jsonl"

CONFIDENCE_GATE = 0.7


class PolicyEngine:
    def __init__(self, service_config: dict, audit_path=None):
        self.service_config = service_config
        self.audit_path = Path(audit_path) if audit_path else POLICY_AUDIT

    def check(self, action: str, confidence: float,
              diagnosis: "DiagnosisResult | None" = None,
              incident_id: str | None = None) -> tuple[bool, str]:
        """判定 (allowed, reason)。任何判定都写审计。incident_id 用于跨文件关联回放。"""
        allowed = False
        reason = ""

        allowed_actions = (self.service_config.get("repair") or {}).get("actions", [])
        process_pattern = (self.service_config.get("process") or {}).get("pattern")

        if action not in allowed_actions:
            reason = f"动作 {action} 不在白名单 {allowed_actions}"
        elif action == "escalate":
            allowed, reason = True, "escalate 永远允许"
        elif action in ("restart", "kill_stale") and confidence < CONFIDENCE_GATE:
            allowed, reason = False, (
                f"置信度 {confidence:.2f} < {CONFIDENCE_GATE}，拒绝自动 {action}（走 escalate）")
        elif action == "kill_stale" and not process_pattern:
            allowed, reason = False, "kill_stale 必须带明确进程 pattern，拒绝"
        else:
            allowed, reason = True, "允许"

        self._audit(action, confidence, diagnosis, allowed, reason, incident_id)
        return allowed, reason

    def _audit(self, action: str, confidence: float,
               diagnosis: "DiagnosisResult | None", allowed: bool, reason: str,
               incident_id: str | None = None) -> None:
        entry = {
            "schema_version": 1,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "service": self.service_config.get("display", "unknown"),
            "incident_id": incident_id,
            "action": action,
            "confidence": round(confidence, 3),
            "root_cause": diagnosis.root_cause if diagnosis else None,
            "allowed": allowed,
            "reason": reason,
        }
        audit_append(self.audit_path, entry)
