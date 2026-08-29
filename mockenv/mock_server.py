#!/usr/bin/env python3
"""mockenv/mock_server.py — HTTP mock 服务（子进程）

隔离评估用：PID 文件 + /healthz + 状态文件驱动的故障模式。
仅监听 127.0.0.1 高位随机端口，绝不触碰真实服务。

用法:
    python mock_server.py --port 12345 --name mock_svc            # 前台运行
    python mock_server.py --port 12345 --name mock_svc --daemon   # 脱离终端（供注入器/评估脚本调用）
"""

import argparse
import atexit
import json
import os
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"      # 状态文件（注入器写入故障模式）
RUN_DIR = BASE / "run"          # PID / 端口文件
CONFIG_DIR = BASE / "config"    # 配置文件（Git 管理的模拟对象）
LOG_DIR = BASE / "logs"         # 访问日志

DEFAULT_CONFIG_TEXT = "name=mock_svc\nrole=mock\n"
DEFAULT_STATE = {
    "modes": [],                 # 激活的故障模式列表（支持 mixed 组合）
    "intermittent": False,       # http_5xx 间歇模式
    "hang_sec": 30,              # timeout / startup_slow 挂起秒数
    "endpoint": "healthz",       # health_fail 作用端点: healthz | cdp
    "disk_pct": 90,              # disk_full 模拟占用百分比
    "dep_port": 0,               # dep_unavailable 依赖端口
    "offset_sec": 3600,          # clock_skew 时间偏移
    "slow_sec": 1.0,             # slow 模式（baseline_slow_31）：/healthz 延迟秒数
    "blip_count": 1,             # startup_blip 模式（baseline_starting_34）：启动期失败次数
}

# Windows 脱离终端标志（DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW）
_DETACH = getattr(subprocess, "DETACHED_PROCESS", 0) | 0x200 | 0x08000000


def load_state(name: str) -> dict:
    """读状态文件；缺失/损坏时返回默认（健康）状态。"""
    state_file = STATE_DIR / f"{name}.json"
    if not state_file.exists():
        return dict(DEFAULT_STATE)
    try:
        merged = dict(DEFAULT_STATE)
        merged.update(json.loads(state_file.read_text(encoding="utf-8")))
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_STATE)


def parse_config(text: str) -> dict:
    """解析 name=value 配置；行格式非法抛 ValueError（模拟配置损坏）。"""
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"malformed config line: {line!r}")
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def log_line(name: str, line: str, state: dict) -> None:
    """写访问日志；clock_skew 模式下时间戳加偏移。"""
    t = time.time()
    if "clock_skew" in state.get("modes", []):
        t += int(state.get("offset_sec", DEFAULT_STATE["offset_sec"]))
    write_file(LOG_DIR / f"{name}.log",
               time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + " " + line + "\n")


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class MockHandler(BaseHTTPRequestHandler):
    server_version = "MockHTTP/1.0"

    def do_GET(self):
        name = self.server.name
        state = load_state(name)
        modes = state.get("modes", [])
        if self.path in ("/healthz", "/healthz/"):
            code, body = self._healthz(state, modes)
        elif self.path in ("/cdp", "/cdp/"):
            code, body = self._cdp(state, modes)
        else:
            code, body = 404, {"status": "not_found"}
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        # warn 模式（baseline_warn_32）：服务正常但访问日志带 WARNING 级记录
        if "warn" in modes:
            log_line(name, f"WARN [{self.command} {self.path} -> {code}] "
                           f"non-fatal condition logged", state)
        else:
            log_line(name, f"{self.command} {self.path} -> {code}", state)

    def _healthz(self, state, modes):
        if "http_500" in modes:
            return 500, {"status": "error", "code": "http_500"}
        if "http_503" in modes:
            if state.get("intermittent"):  # 间歇性 503（mixed_29）
                self.server._alt = not getattr(self.server, "_alt", False)
                if not self.server._alt:
                    return 200, {"status": "ok"}
            return 503, {"status": "error", "code": "http_503"}
        if "health_fail" in modes and state.get("endpoint", "healthz") == "healthz":
            return 503, {"status": "unhealthy"}  # 进程在但健康检查失败
        if "startup_blip" in modes:  # baseline_starting_34：启动期单次失败后恢复（非故障）
            if self.server._blip_remaining is None:
                self.server._blip_remaining = int(state.get("blip_count", DEFAULT_STATE["blip_count"]))
            if self.server._blip_remaining > 0:
                self.server._blip_remaining -= 1
                return 503, {"status": "error", "code": "startup_blip"}
        if "slow" in modes:  # baseline_slow_31：响应慢但仍 200（探针超时内应通过）
            time.sleep(float(state.get("slow_sec", DEFAULT_STATE["slow_sec"])))
        if "timeout" in modes:
            time.sleep(int(state.get("hang_sec", DEFAULT_STATE["hang_sec"])))  # 挂起不响应
        if "dep_unavailable" in modes:
            dep_port = int(state.get("dep_port", 0) or 0)
            if dep_port and not _port_open("127.0.0.1", dep_port):
                return 503, {"status": "unhealthy", "reason": "dependency_unavailable"}
        extra = {}
        if "disk_full" in modes:
            extra["disk_pct"] = int(state.get("disk_pct", DEFAULT_STATE["disk_pct"]))
        if "clock_skew" in modes:
            extra["server_time"] = int(time.time()) + int(state.get("offset_sec", DEFAULT_STATE["offset_sec"]))
        return 200, {"status": "ok", **extra}

    def _cdp(self, state, modes):
        if "cdp_down" in modes:
            return 503, {"status": "error", "code": "cdp_down"}
        if "health_fail" in modes and state.get("endpoint") == "cdp":
            return 503, {"status": "unhealthy", "endpoint": "cdp"}
        return 200, {"status": "ok", "endpoint": "cdp"}

    def log_message(self, *args):
        pass  # 访问日志由 log_line 统一处理


def serve(port: int, name: str) -> None:
    state = load_state(name)
    modes = state.get("modes", [])

    # --- 启动期故障模式（直接退出 = 启动失败） ---
    if "startup_loop" in modes:
        print(f"[{name}] startup_loop: 启动即崩溃", file=sys.stderr)
        sys.exit(1)
    if "config_missing" in modes:
        print(f"[{name}] config_missing: 配置文件缺失，启动失败", file=sys.stderr)
        sys.exit(1)
    if "config_malformed" in modes:
        print(f"[{name}] config_malformed: 配置文件损坏，启动失败", file=sys.stderr)
        sys.exit(1)

    # --- 配置文件检查/初始化 ---
    config_path = CONFIG_DIR / f"{name}.conf"
    if config_path.exists():
        try:
            parse_config(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"[{name}] 配置文件解析失败: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        write_file(config_path, DEFAULT_CONFIG_TEXT)  # 全新环境自动初始化默认配置

    # --- PID / 端口文件（先于监听写出，进程存在即视为已启动） ---
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = RUN_DIR / f"{name}.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: pid_file.unlink(missing_ok=True))
    (RUN_DIR / f"{name}.port").write_text(str(port), encoding="utf-8")

    if "startup_slow" in modes:
        print(f"[{name}] startup_slow: 挂起 {state.get('hang_sec')}s 后才监听", file=sys.stderr)
        time.sleep(int(state.get("hang_sec", DEFAULT_STATE["hang_sec"])))

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    except OSError as e:
        print(f"[{name}] 绑定端口 {port} 失败: {e}", file=sys.stderr)
        sys.exit(2)
    server.daemon_threads = True
    server.name = name
    server._alt = False
    server._blip_remaining = None  # startup_blip 模式剩余失败次数（None=未初始化）
    print(f"[{name}] listening on 127.0.0.1:{port} (pid {os.getpid()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP mock 服务（隔离评估用）")
    parser.add_argument("--port", type=int, default=0, help="监听端口，默认随机高位端口 10000-60000")
    parser.add_argument("--name", default="mock_svc", help="服务名，用于 PID 文件命名")
    parser.add_argument("--daemon", action="store_true", help="脱离终端运行（父进程退出，子进程继续）")
    args = parser.parse_args()

    if args.daemon:
        # 以脱离终端方式重新拉起自身（不带 --daemon），父进程立即退出
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             "--port", str(args.port), "--name", args.name],
            creationflags=_DETACH, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return

    port = args.port
    if not port:
        while True:  # 随机高位端口
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            if 10000 <= port <= 60000:
                break
    serve(port, args.name)


if __name__ == "__main__":
    main()
