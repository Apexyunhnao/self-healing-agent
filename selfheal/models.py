"""selfheal/models.py — 共享数据模型。

诊断（LLM 与 Rule 同 schema）、Incident（故障生命周期记录）、TickResult（单次检查结果）。
全部 dataclass，无逻辑。
"""

from dataclasses import dataclass, field


@dataclass
class DiagnosisResult:
    """诊断结果：LLM 与 Rule 共用同一 schema。"""

    root_cause: str            # 根因描述
    confidence: float          # 0-1，<0.7 走保守动作
    suggested_action: str      # 必须在白名单内
    reason: str                # 理由
    diagnoser: str = "rule"    # "rule" / "llm"


@dataclass
class Incident:
    """一次故障生命周期记录（写入 audit/incidents.jsonl）。"""

    service: str
    incident_id: str
    state_path: list = field(default_factory=list)
    action: str | None = None
    ok: bool = False
    started_at: str = ""
    ended_at: str = ""


@dataclass
class TickResult:
    """单次检查循环的结果快照。"""

    service: str
    state: str
    probe_ok: bool
    diagnosis: "DiagnosisResult | None" = None
    action: str | None = None
    verified: bool | None = None
