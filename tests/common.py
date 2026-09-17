"""tests/common.py — 单测共享工具（mock 服务启停/清理/端口）。"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MOCKENV = BASE / "mockenv"
RUN_DIR = MOCKENV / "run"
STATE_DIR = MOCKENV / "state"
DB_FILE = STATE_DIR / "mock_svc.db"
PY = sys.executable


def free_port() -> int:
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if 10000 <= p <= 60000:
            return p


def run_script(script, args=(), timeout: int = 120):
    return subprocess.run([PY, str(MOCKENV / script), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def is_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10)
        return f'"{pid}"' in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=15)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def http_get(port: int, path: str = "/healthz", timeout: float = 3.0):
    import httpx
    # trust_env=False：不走系统代理（V2RayN 拦截 localhost 的坑）
    try:
        r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=timeout, trust_env=False)
        return r.status_code, r.text
    except Exception as e:
        return -1, str(e)


def start_mock(port: int, name: str = "mock_svc") -> None:
    (RUN_DIR / f"{name}.pid").unlink(missing_ok=True)
    run_script("mock_server.py", ["--port", str(port), "--name", name, "--daemon"])


def wait_healthz(port: int, timeout: float = 15.0, name: str = "mock_svc") -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid_file = RUN_DIR / f"{name}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_alive(pid):
                code, body = http_get(port)
                if code == 200 and '"status": "ok"' in body:
                    return True
        time.sleep(0.4)
    return False


def kill_mock_processes() -> None:
    """杀掉所有命令行含 mock_server.py 的残留进程（含未写 PID 文件的）。

    Windows 上 wmic call terminate 是异步的（返回后进程可能还活着，
    延迟 terminate 会误杀后续刚启动的 mock——见 skill 残留串台坑）。
    改用 wmic 列 PID + taskkill /F /T（同步，杀完才返回）。
    """
    if os.name == "nt":
        # wmic 的 where 条件里含 'mock_server.py' 字符串，会匹配到执行 wmic
        # 的进程链自己（bash/cmd/wmic 的命令行都含该字符串）→ taskkill 误杀调用方。
        # 加 NOT like '%wmic%' 排除：真 mock_server 命令行不含 'wmic'。
        out = subprocess.run(
            ["wmic", "process", "where",
             "CommandLine like '%mock_server.py%' and CommandLine not like '%wmic%'",
             "get", "ProcessId"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15)
        pids = [int(t) for t in out.stdout.split() if t.strip().isdigit()]
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
    for pid_file in RUN_DIR.glob("*.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            continue
        if is_alive(pid):
            kill_pid(pid)
        pid_file.unlink(missing_ok=True)


CONFIG_DIR = MOCKENV / "config"
MOCK_CONFIG = CONFIG_DIR / "mock_svc.conf"
DEFAULT_CONFIG_TEXT = "name=mock_svc\nrole=mock\n"


def config_is_valid(path: Path) -> bool:
    """配置是否可解析（name=value 格式，UTF-8 可解码）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" not in line:
            return False
    return True


def restore_config() -> None:
    """恢复合法配置（config_malformed 注入可能把 .conf 写坏，且 .bak 也可能被污染）。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if MOCK_CONFIG.exists() and config_is_valid(MOCK_CONFIG):
        return
    MOCK_CONFIG.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    (CONFIG_DIR / "mock_svc.conf.bak").unlink(missing_ok=True)


def cleanup_state() -> None:
    """清服务状态/DB/注入标记/配置损坏，防止残留影响下次测试。"""
    for f in (list(STATE_DIR.glob("mock_svc.json"))
              + list(STATE_DIR.glob("mock_db.json"))
              + list(STATE_DIR.glob("injected_*.json"))
              + list(STATE_DIR.glob("disk_fill_*.bin"))):
        f.unlink(missing_ok=True)
    (RUN_DIR / "mock_svc.port").unlink(missing_ok=True)
    restore_config()
