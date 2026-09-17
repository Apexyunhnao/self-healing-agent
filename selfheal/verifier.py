"""selfheal/verifier.py — 确定性验证器。

证明修复真实生效：HTTP / 进程 / DB。重试 + 指数退避（默认 1s/2s/4s 序列）。
HTTP 验证要求连续 samples 次 200（默认 3 次、间隔 0.5s），防止间歇故障在
"好窗口"骗过验证器。只宣布 ok 当且仅当确定性检查通过。禁止调用 LLM。
所有 httpx 请求 trust_env=False。
"""

import time
from dataclasses import dataclass

from selfheal.probe import DBProbe, HTTPProbe, ProcessProbe


@dataclass
class VerifyResult:
    ok: bool
    attempts: int
    detail: str
    samples: int = 1


def _sleep_backoff(backoff: float, attempt: int) -> None:
    """指数退避：第 attempt 次失败后睡 backoff * 2**(attempt-1)。"""
    if backoff > 0:
        time.sleep(backoff * (2 ** (attempt - 1)))


def verify_http(url: str, timeout: float = 5.0, retries: int = 3,
                backoff: float = 1.0, samples: int = 3,
                sample_interval: float = 0.5) -> VerifyResult:
    """HTTP 200 验证：连续 samples 次都 200 才算 ok，失败按指数退避重试。

    samples>1 时，一次「尝试」= 连续 samples 次探测；中途任何一次非 200 即本次
    尝试作废（间歇故障无法用单次好窗口骗过）。返回实际尝试次数与采样数。
    """
    probe = HTTPProbe(url, timeout=timeout)
    last = None
    for attempt in range(1, retries + 1):
        good = 0
        fail_detail = None
        for i in range(samples):
            r = probe.check()
            if not r.ok:
                fail_detail = f"status={r.status} {r.detail}"
                break
            good += 1
            if i < samples - 1:
                time.sleep(sample_interval)
        if good == samples:
            return VerifyResult(True, attempt,
                                f"连续 {samples} 次 HTTP 200 after {attempt} 次尝试", samples)
        last = fail_detail or "unknown"
        if attempt < retries:
            _sleep_backoff(backoff, attempt)
    return VerifyResult(False, retries, f"重试 {retries} 次仍失败: {last}", samples)


def verify_process(name: str, pid_file: str | None = None) -> VerifyResult:
    """进程存活验证（单次，attempts=1）。"""
    r = ProcessProbe(name=name, pid_file=pid_file).check()
    return VerifyResult(r.ok, 1, r.detail if not r.ok else f"进程存活 {r.detail}")


def verify_db(path: str, timeout: float = 2.0) -> VerifyResult:
    """DB 可打开 + integrity_check 验证（单次，attempts=1）。"""
    r = DBProbe(path, timeout=timeout).check()
    return VerifyResult(r.ok, 1, r.detail if not r.ok else f"DB 健康 {r.detail}")


def verify(kind: str, **kwargs) -> VerifyResult:
    """统一验证入口：verify('http', url=...) / verify('process', name=...) / verify('db', path=...)。"""
    if kind == "http":
        return verify_http(**kwargs)
    if kind == "process":
        return verify_process(kwargs.get("name"), kwargs.get("pid_file"))
    if kind == "db":
        return verify_db(kwargs.get("path"), kwargs.get("timeout", 2.0))
    raise ValueError(f"未知验证类型: {kind}")
