"""selfheal/llm_diagnoser.py — LLM 诊断器（唯一 LLM 入口）。

- 读项目根 .env 的 DEEPSEEK_API_KEY；没有 key → 返回 None（上层走 Rule fallback）。
- 调 DeepSeek（OpenAI 兼容 https://api.deepseek.com），httpx trust_env=False，超时 30s。
- 输出 JSON：{root_cause, confidence, suggested_action, reason}；解析失败/动作不在
  白名单 → 返回 None（降级 Rule），绝不让 LLM 输出绕过策略门。
- 每次调用记录原始输出与解析结果到 audit/llm_raw.jsonl（评估用）。
- 每次调用记录 (timestamp, input_tokens, output_tokens, duration) 到
  audit/llm_calls.jsonl（评估报表 LLM 成本估算用）。
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

from selfheal.models import DiagnosisResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_AUDIT = PROJECT_ROOT / "audit" / "llm_raw.jsonl"
CALLS_AUDIT = PROJECT_ROOT / "audit" / "llm_calls.jsonl"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"  # DeepSeek 官方 chat 类模型；deepseek-v4-flash 亦属 chat 类
WHITELIST = ("restart", "kill_stale", "cleanup_logs", "git_revert_config", "escalate")
SYSTEM_PROMPT = (
    "你是本地服务自愈 Agent 的诊断器。根据探针结果、服务类型和日志片段，"
    "判断根因并给出修复建议。只允许输出 JSON，不要输出任何其他文字。"
    "suggested_action 必须且只能从以下白名单选择：restart / kill_stale / "
    "cleanup_logs / git_revert_config / escalate。confidence 取 0-1，"
    "不确定时给低置信度，让策略门保守放行。"
)


def load_api_key(env_path=None) -> str | None:
    """读 .env 或环境变量的 DEEPSEEK_API_KEY；空值返回 None。"""
    path = Path(env_path) if env_path else PROJECT_ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return os.environ.get("DEEPSEEK_API_KEY", "").strip() or None


class LLMDiagnoser:
    def __init__(self, service_config: dict, api_key: str | None = None,
                 model: str | None = None, base_url: str | None = None,
                 timeout: float = 30.0):
        self.service_config = service_config
        self.api_key = api_key if api_key is not None else load_api_key()
        self.model = model or DEFAULT_MODEL
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.call_count = 0  # 诊断建议次数（评估报表"诊断建议总次数"用）

    def diagnose(self, probe_result, log_tail: str = "") -> DiagnosisResult | None:
        """诊断入口。无 key → None；调用/解析失败 → None（上层 Rule fallback）。"""
        if not self.api_key:
            print("[llm_diagnoser] 未配置 DEEPSEEK_API_KEY，LLM 诊断降级到 Rule")
            return None

        prompt = self._build_prompt(probe_result, log_tail[:2000])
        raw, usage, duration_s = self._call(prompt)
        self.call_count += 1
        parsed = self._parse(raw)
        self._audit(raw, parsed)
        self._audit_call(usage.get("input_tokens", 0),
                         usage.get("output_tokens", 0), duration_s)
        return parsed

    # ---- 内部 ----

    def _build_prompt(self, probe_result, log_tail: str) -> str:
        svc = self.service_config
        probe = svc.get("probe") or {}
        return (
            "服务: {display} (probe type={ptype}, url={url})\n"
            "探针结果: ok={ok}, status={status}, detail={detail}\n"
            "最近日志片段 (最多 2000 字符):\n{tail}\n"
            "请输出 JSON: {{\"root_cause\": str, \"confidence\": float, "
            "\"suggested_action\": str, \"reason\": str}}"
        ).format(
            display=svc.get("display", svc.get("name", "unknown")),
            ptype=probe.get("type", "http"), url=probe.get("url", ""),
            ok=probe_result.ok, status=probe_result.status,
            detail=probe_result.detail[:200], tail=(log_tail or "(无日志)")[:2000],
        )

    def _call(self, prompt: str) -> tuple[str | None, dict, float]:
        """调 DeepSeek chat completions；返回 (raw, usage, duration_s)。

        任何异常返回 (None, {}, 耗时)，调用方仍会记账（成本报表按次数统计）。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        t0 = time.perf_counter()
        try:
            r = httpx.post(url, json=payload, headers=headers,
                           timeout=self.timeout, trust_env=False)
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage") or {}
            raw = data["choices"][0]["message"]["content"]
            return raw, {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            }, round(time.perf_counter() - t0, 3)
        except Exception as e:
            print(f"[llm_diagnoser] DeepSeek 调用失败: {e}")
            return None, {}, round(time.perf_counter() - t0, 3)

    def _parse(self, raw: str | None) -> DiagnosisResult | None:
        """解析 LLM 输出；JSON 非法 / 缺字段 / 动作不在白名单 / 置信度越界 → None。"""
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        try:
            root_cause = str(obj["root_cause"]).strip()
            confidence = float(obj["confidence"])
            action = str(obj["suggested_action"]).strip()
            reason = str(obj.get("reason", "")).strip()
        except (KeyError, TypeError, ValueError):
            return None
        if action not in WHITELIST:
            print(f"[llm_diagnoser] 动作不在白名单: {action} → 降级 Rule")
            return None
        if not 0.0 <= confidence <= 1.0:
            return None
        return DiagnosisResult(root_cause, confidence, action, reason, "llm")

    def _audit(self, raw: str | None, parsed: DiagnosisResult | None) -> None:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "service": self.service_config.get("display", "unknown"),
            "raw": raw,
            "parsed": (None if parsed is None else {
                "root_cause": parsed.root_cause,
                "confidence": parsed.confidence,
                "suggested_action": parsed.suggested_action,
                "reason": parsed.reason,
            }),
        }
        try:
            RAW_AUDIT.parent.mkdir(parents=True, exist_ok=True)
            with RAW_AUDIT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:  # 审计失败不影响诊断
            pass

    def _audit_call(self, input_tokens: int, output_tokens: int, duration_s: float) -> None:
        """成本审计：每次 LLM 调用追加一行到 audit/llm_calls.jsonl。"""
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "service": self.service_config.get("display", "unknown"),
            "model": self.model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_s": duration_s,
        }
        try:
            CALLS_AUDIT.parent.mkdir(parents=True, exist_ok=True)
            with CALLS_AUDIT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:  # 审计失败不影响诊断
            pass
