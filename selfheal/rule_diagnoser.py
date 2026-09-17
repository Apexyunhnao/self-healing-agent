"""selfheal/rule_diagnoser.py — 规则诊断器（baseline，确定性，不用 LLM）。

按探针结果映射根因 → 建议动作 + 置信度。输出与 LLM 同 schema（DiagnosisResult）。
置信度刻意设保守值：http 5xx=0.8（restart 清状态文件后状态型故障可修复）、
unhealthy_but_alive=0.5，后者让策略门拒绝自动 restart——「LLM 可以提出，
但只有确定性策略允许执行」的兜底演示。

进程存活判断：优先 pid_file（ProcessProbe），否则按 service.yaml process.pattern
做命令行匹配（复用 executor._find_pids，能匹配 python.exe 起的 mock 进程）。
"""

from selfheal.executor import _find_pids
from selfheal.models import DiagnosisResult
from selfheal.probe import ProbeResult, ProcessProbe


class RuleDiagnoser:
    def __init__(self, service_config: dict):
        self.service_config = service_config
        self.call_count = 0  # 诊断建议次数（评估报表"诊断建议总次数"用）

    def diagnose(self, probe_result: ProbeResult) -> DiagnosisResult:
        self.call_count += 1
        probe_type = (self.service_config.get("probe") or {}).get("type", "http")
        status = probe_result.status or ""

        if probe_type == "http":
            # 连接失败/超时 + 进程不在 → 进程挂了
            if status == "network_error":
                alive = self._process_alive()
                if alive is False:
                    return DiagnosisResult(
                        "process_down", 0.9, "restart",
                        "HTTP 连接失败且进程探针失败 → 进程 down", "rule")
                return DiagnosisResult(
                    "unhealthy_but_alive", 0.5, "restart",
                    "进程在但 HTTP 连接失败 → 保守，策略门会拦", "rule")
            # HTTP 5xx → 状态型故障，restart 清 state_file 后可修复（置信度 ≥0.7 放行）
            if self._is_5xx(status):
                return DiagnosisResult(
                    "http_error", 0.8, "restart",
                    "HTTP 5xx 状态型故障，restart 清运行时状态后应可修复", "rule")

        if probe_type == "db" and status in ("db_integrity", "db_error"):
            return DiagnosisResult(
                "db_corrupt", 0.8, "escalate",
                "DB 完整性失败，v1 不自动重构", "rule")

        if probe_type == "disk" and not probe_result.ok:
            return DiagnosisResult(
                "disk_full", 0.9, "cleanup_logs",
                "磁盘占用超阈值", "rule")

        return DiagnosisResult(
            "unknown", 0.3, "escalate",
            "无匹配规则，保守升级人工", "rule")

    # ---- 内部 ----

    @staticmethod
    def _is_5xx(status: str) -> bool:
        if not status.startswith("http_"):
            return False
        code = status.split("_", 1)[1]
        return code.isdigit() and int(code) >= 500

    def _process_alive(self) -> bool | None:
        """进程存活判断；无 process 配置时返回 None（未知）。"""
        process = self.service_config.get("process") or {}
        pid_file = process.get("pid_file")
        if pid_file:
            return ProcessProbe(pid_file=str(pid_file)).check().ok
        pattern = process.get("pattern")
        if pattern:
            return bool(_find_pids(pattern))
        return None
