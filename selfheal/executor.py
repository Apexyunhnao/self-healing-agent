"""selfheal/executor.py — 确定性修复执行器。

白名单动作（全部从 service.yaml 读配置，幂等）：
    restart / kill_stale / cleanup_logs / git_revert_config / escalate
安全红线：只按 service.yaml 声明的 process.pattern 杀进程，绝不杀未知进程。
所有 subprocess.run 带 encoding="utf-8", errors="replace"，且必设超时。
"""

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from selfheal.probe import _find_pids, _is_pid_alive

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_LOG = PROJECT_ROOT / "audit_log.jsonl"
CMD_TIMEOUT = 15
# Windows 脱离终端标志（DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW）
_DETACH = getattr(subprocess, "DETACHED_PROCESS", 0) | 0x200 | 0x08000000
WHITELIST = ("restart", "kill_stale", "cleanup_logs", "git_revert_config", "escalate")


@dataclass
class ActionResult:
    ok: bool
    action: str
    detail: str


# ---------- 进程定位/终止（只针对 service.yaml 声明的 pattern） ----------

def _kill_pids(pids: list) -> int:
    """强杀进程并等待退出；返回实际杀掉数量。"""
    if not pids:
        return 0
    for pid in pids:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=CMD_TIMEOUT)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    killed = 0
    for _ in range(40):  # 最多等 10s 进程退出
        alive = [p for p in pids if _is_pid_alive(p)]
        killed = len(pids) - len(alive)
        if not alive:
            break
        time.sleep(0.25)
    return killed


# ---------- 白名单动作 ----------

def _clear_state_file(service: dict) -> None:
    """restart 前清运行时状态文件（模拟"重启清内存"）：存在才删，缺失无害。

    state_file 路径支持 ${MOCK_PORT} 变量替换——config.load_services 已先解析，
    这里兜底再解析一次（供绕过 config 直接构造的 service dict 用）。
    """
    sf = service.get("state_file")
    if not sf:
        return
    sf = re.sub(r"\$\{MOCK_PORT\}", os.environ.get("MOCK_PORT", "18080"), sf)
    p = Path(sf)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def restart(service: dict) -> ActionResult:
    """重启服务（幂等）：活着才杀，再用 start_command 启动；不等待就绪（由 verifier 管）。"""
    pattern = (service.get("process") or {}).get("pattern")
    if not pattern:
        return ActionResult(False, "restart", "缺少 process.pattern，无法定位进程")
    start_cmd = service.get("start_command")
    if not start_cmd:
        return ActionResult(False, "restart", "缺少 start_command，无法启动")

    alive = _find_pids(pattern)
    killed = _kill_pids(alive) if alive else 0
    _clear_state_file(service)   # 清运行时状态：重启 = 清内存，状态型故障随之消失
    try:
        proc = subprocess.Popen(
            start_cmd, cwd=str(PROJECT_ROOT), creationflags=_DETACH,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        return ActionResult(False, "restart", f"启动失败: {e}")
    # 启动器（--daemon 包装）若立即退出则回收句柄，避免 ResourceWarning；
    # 真服务常驻进程 2s 内不退则放行，不阻塞等待就绪（就绪由 verifier 管）。
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    return ActionResult(True, "restart", f"killed={killed} spawned=pid{proc.pid}")


def kill_stale(service: dict) -> ActionResult:
    """按命令行 pattern 杀残留进程；无残留也算成功（幂等）。"""
    pattern = (service.get("process") or {}).get("pattern")
    if not pattern:
        return ActionResult(False, "kill_stale", "缺少 process.pattern")
    pids = _find_pids(pattern)
    if not pids:
        return ActionResult(True, "kill_stale", "无残留进程")
    killed = _kill_pids(pids)
    return ActionResult(True, "kill_stale", f"killed={killed} pids={pids}")


def cleanup_logs(service: dict, size_bytes: int = 1 * 1024 * 1024) -> ActionResult:
    """删除 log_dir 下超过 size_bytes 的文件；目录不存在或未配置则失败。"""
    log_dir = service.get("log_dir")
    if not log_dir:
        return ActionResult(False, "cleanup_logs", "未配置 log_dir")
    d = Path(log_dir)
    if not d.is_dir():
        return ActionResult(False, "cleanup_logs", f"log_dir 不存在: {d}")
    removed = []
    for f in d.iterdir():
        if f.is_file():
            try:
                if f.stat().st_size > size_bytes:
                    f.unlink()
                    removed.append(f.name)
            except OSError:
                pass
    return ActionResult(True, "cleanup_logs",
                        f"removed={len(removed)} {removed[:5]}")


def git_revert_config(service: dict) -> ActionResult:
    """在 config_dir（git 仓库）执行 git reset --hard HEAD~1 回滚配置。"""
    config_dir = service.get("config_dir")
    if not config_dir:
        return ActionResult(False, "git_revert_config", "未配置 config_dir（非 git 管理）")
    d = Path(config_dir)
    if not (d / ".git").exists():
        return ActionResult(False, "git_revert_config", f"{d} 不是 git 仓库")
    cp = subprocess.run(
        ["git", "reset", "--hard", "HEAD~1"], cwd=str(d), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if cp.returncode != 0:
        return ActionResult(False, "git_revert_config",
                            f"git reset 失败: {cp.stderr.strip()}")
    return ActionResult(True, "git_revert_config", cp.stdout.strip())


def escalate(service: dict, reason: str) -> ActionResult:
    """升级人工：把事件追加到 audit log（JSON 行）。"""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": "escalate",
        "service": service.get("display", "unknown"),
        "reason": reason,
    }
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return ActionResult(True, "escalate", f"已追加 audit log: {AUDIT_LOG}")
    except OSError as e:
        return ActionResult(False, "escalate", str(e))


_HANDLERS = {
    "restart": restart,
    "kill_stale": kill_stale,
    "cleanup_logs": cleanup_logs,
    "git_revert_config": git_revert_config,
    "escalate": escalate,
}


def execute(action_name: str, service_config: dict, **kwargs) -> ActionResult:
    """白名单动作统一入口。

    Args:
        action_name: 白名单动作名
        service_config: load_services 得到的单个服务配置 dict
        **kwargs: 动作特有参数（如 escalate 的 reason）
    """
    if action_name not in WHITELIST:
        return ActionResult(False, action_name, f"动作不在白名单: {action_name}")
    return _HANDLERS[action_name](service_config, **kwargs)
