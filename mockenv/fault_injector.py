#!/usr/bin/env python3
"""mockenv/fault_injector.py — 故障注入器

按 mockenv/scenarios/fault_matrix.json 执行/恢复场景注入。
注入前检查目标存在，注入后写 state/injected_<scenario_id>.json 标记，
recover 按标记清理（标记缺失时尽力而为）。

用法:
    python fault_injector.py list
    python fault_injector.py inject <scenario_id> [--dry-run]
    python fault_injector.py recover <scenario_id> [--dry-run]
    python fault_injector.py recover --all        # 清理所有残留注入
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
RUN_DIR = BASE / "run"
CONFIG_DIR = BASE / "config"
LOG_DIR = BASE / "logs"
MATRIX = BASE / "scenarios" / "fault_matrix.json"
CMD_TIMEOUT = 15  # 所有外部命令超时（秒）
PY = sys.executable
_DETACH = getattr(subprocess, "DETACHED_PROCESS", 0) | 0x200 | 0x08000000
DEFAULT_CONFIG_TEXT = "name=mock_svc\nrole=mock\n"


def config_is_valid(cfg: Path) -> bool:
    """配置是否可解析（name=value 格式，UTF-8 可解码）。"""
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" not in line:
            return False
    return True


def backup_config(target: str) -> None:
    """写 .bak 备份；目标 config 已损坏时备份干净默认内容（否则 recover 恢复不出坏配置）。"""
    cfg = CONFIG_DIR / f"{target}.conf"
    backup = CONFIG_DIR / f"{target}.conf.bak"
    if config_is_valid(cfg):
        backup.write_bytes(cfg.read_bytes())
    else:
        backup.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


KNOWN_METHODS = {
    "kill_pid", "occupy_port", "http_5xx", "http_timeout",
    "config_missing", "config_malformed", "db_corrupt", "db_lock",
    "disk_full_sim", "log_flood", "startup_loop", "startup_slow",
    "health_fail", "cdp_down", "cpu_spike", "mem_leak", "clock_skew",
    "dep_unavailable", "none",
}


# ---------- 基础工具 ----------

def load_matrix() -> list:
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    if len(scenarios) != 35:
        raise ValueError(f"fault_matrix.json 场景数 = {len(scenarios)}，必须为 35")
    return scenarios


def get_scenario(scenario_id: str) -> dict:
    for s in load_matrix():
        if s["scenario_id"] == scenario_id:
            return s
    raise ValueError(f"未知场景: {scenario_id}")


def build_actions(scenario: dict) -> list:
    """主动作 + mixed 场景的 params.actions 组合动作。"""
    inj = scenario["inject"]
    primary = {"method": inj["method"], "target": inj["target"], "params": inj.get("params", {})}
    extra = primary["params"].get("actions", []) if isinstance(primary["params"], dict) else []
    actions = [primary] + [dict(a) for a in extra]
    for a in actions:
        if a["method"] not in KNOWN_METHODS:
            raise ValueError(f"不支持注入方法: {a['method']}")
    return actions


def run_script(script: Path, args: list) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(script), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=CMD_TIMEOUT)


def spawn_detached(args: list) -> subprocess.Popen:
    return subprocess.Popen(args, creationflags=_DETACH, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def is_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10)
        return f'"{pid}"' in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def kill_by_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=CMD_TIMEOUT)
    else:
        os.kill(pid, signal.SIGKILL)
    for _ in range(20):  # 等进程真正退出
        if not is_alive(pid):
            return
        time.sleep(0.25)


def service_pid(name: str):
    pid_file = RUN_DIR / f"{name}.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def service_port(name: str):
    port_file = RUN_DIR / f"{name}.port"
    if not port_file.exists():
        return None
    try:
        return int(port_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def kill_service_if_alive(name: str):
    """杀掉运行中的服务进程（startup 类故障需进程先死，故障才在重启时暴露）。

    按 pid 文件 + 命令行 pattern 双保险：pid 文件只记录 uv 子进程，
    venv shim 父进程（命令行同样含 mock_server.py）可能残留，必须一并清，
    否则注入后探针/诊断会误判进程仍存活（unhealthy_but_alive 分叉）。
    """
    pid = service_pid(name)
    if pid and is_alive(pid):
        kill_by_pid(pid)
    # 命令行形如 `...mock_server.py --port X --name mock_svc`，--name 前隔着 --port，
    # 单条件 'mock_server.py --name mock_svc' 匹配不到，必须拆成两个 like 条件。
    out = subprocess.run(
        ["wmic", "process", "where",
         f"CommandLine like '%mock_server.py%' and CommandLine like '%--name {name}%'"
         " and CommandLine not like '%wmic%'",
         "get", "ProcessId"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=CMD_TIMEOUT)
    for p in [int(t) for t in out.stdout.split() if t.strip().isdigit()]:
        if is_alive(p):
            kill_by_pid(p)


def load_state(name: str) -> dict:
    state_file = STATE_DIR / f"{name}.json"
    if not state_file.exists():
        return {"modes": []}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"modes": []}


def save_state(name: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def add_mode(name: str, mode: str, ctx: dict) -> None:
    """加故障模式到状态文件，并记录注入前的状态（recover 用）。"""
    if name not in ctx["state_before"]:
        ctx["state_before"][name] = load_state(name)
    st = load_state(name)
    st.setdefault("modes", [])
    if mode not in st["modes"]:
        st["modes"].append(mode)
    save_state(name, st)


def set_param(name: str, key: str, value) -> None:
    st = load_state(name)
    st[key] = value
    save_state(name, st)


def marker_path(scenario_id: str) -> Path:
    return STATE_DIR / f"injected_{scenario_id}.json"


# ---------- 注入 ----------

def apply_inject(method: str, target: str, params: dict, ctx: dict) -> None:
    """执行单个注入动作，把需要恢复的信息（pid/备份/文件/旧状态）写进 ctx。"""
    if method == "none":
        return

    if method == "kill_pid":
        pid = service_pid(target)
        if pid is None:
            raise RuntimeError(f"[{target}] PID 文件不存在，无法注入 kill_pid")
        if is_alive(pid):
            kill_by_pid(pid)
        elif not params.get("stale"):
            raise RuntimeError(f"[{target}] 进程 {pid} 已死（注入前应处于健康态，先 recover）")
        # kill 后 PID 文件自然残留 = 僵尸 PID 文件（proc_stale_03 语义）

    elif method == "occupy_port":
        port = service_port(target)
        if port is None:
            raise RuntimeError(f"[{target}] 端口文件不存在，无法注入 occupy_port")
        kill_service_if_alive(target)  # 先让端口空出来，再被占
        if params.get("type", "managed") == "managed":
            proc = spawn_detached([PY, str(BASE / "mock_server.py"),
                                   "--port", str(port), "--name", "mock_svc_dup"])
        else:  # unknown：裸 socket 假进程（非托管，Agent 不应杀）
            code = ("import socket,time; s=socket.socket(); "
                    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
                    f"s.bind(('127.0.0.1', {port})); s.listen(1); time.sleep(86400)")
            proc = spawn_detached([PY, "-c", code])
        ctx["pids"].append(proc.pid)

    elif method == "http_5xx":
        add_mode(target, "http_500" if str(params.get("code", 500)) == "500" else "http_503", ctx)
        if params.get("intermittent"):
            set_param(target, "intermittent", True)

    elif method == "http_timeout":
        add_mode(target, "timeout", ctx)
        set_param(target, "hang_sec", int(params.get("hang_sec", 30)))

    elif method == "health_fail":
        add_mode(target, "health_fail", ctx)
        set_param(target, "endpoint", params.get("endpoint", "healthz"))

    elif method == "cdp_down":
        add_mode(target, "cdp_down", ctx)

    elif method == "clock_skew":
        add_mode(target, "clock_skew", ctx)
        set_param(target, "offset_sec", int(params.get("offset_sec", 3600)))

    elif method == "dep_unavailable":
        add_mode(target, "dep_unavailable", ctx)
        set_param(target, "dep_port", int(params.get("dep_port", 0)))

    elif method == "disk_full_sim":
        add_mode(target, "disk_full", ctx)
        set_param(target, "disk_pct", int(params.get("pct", 90)))
        fill = STATE_DIR / f"disk_fill_{target}.bin"
        with fill.open("wb") as f:
            f.truncate(int(params.get("size_mb", 100)) * 1024 * 1024)  # 默认 100MB
        ctx["files"].append(str(fill))

    elif method == "config_missing":
        cfg = CONFIG_DIR / f"{target}.conf"
        if not cfg.exists():
            raise RuntimeError(f"[{target}] config 文件不存在，无法注入 config_missing")
        backup_config(target)
        backup = CONFIG_DIR / f"{target}.conf.bak"
        cfg.unlink()
        ctx["backups"][str(cfg)] = str(backup)
        add_mode(target, "config_missing", ctx)
        kill_service_if_alive(target)

    elif method == "config_malformed":
        cfg = CONFIG_DIR / f"{target}.conf"
        if not cfg.exists():
            raise RuntimeError(f"[{target}] config 文件不存在，无法注入 config_malformed")
        backup_config(target)
        backup = CONFIG_DIR / f"{target}.conf.bak"
        cfg.write_bytes(b"\x00\xff broken config, not name=value\n" * 8)
        ctx["backups"][str(cfg)] = str(backup)
        add_mode(target, "config_malformed", ctx)
        kill_service_if_alive(target)

    elif method == "db_corrupt":
        r = run_script(BASE / "mock_db.py", ["--corrupt"])
        if r.returncode != 0:
            raise RuntimeError(f"mock_db --corrupt 失败: {r.stderr.strip()}")

    elif method == "db_lock":
        run_script(BASE / "mock_db.py", ["--init"])
        proc = spawn_detached([PY, str(BASE / "mock_db.py"), "--lock"])
        ctx["pids"].append(proc.pid)
        # 等待锁生效：轮询 DB 打开失败（最多 5 秒），避免调用方立即检测时锁还没拿到
        import sqlite3 as _sqlite3
        _db = BASE / "state" / "mock_svc.db"
        _deadline = time.time() + 5.0
        while time.time() < _deadline:
            try:
                _c = _sqlite3.connect(str(_db), timeout=0.3)
                _c.execute("SELECT 1")
                _c.close()
                time.sleep(0.3)
            except _sqlite3.Error:
                break
        else:
            raise RuntimeError("db_lock 注入超时：锁未生效")

    elif method == "log_flood":
        log = LOG_DIR / f"{target}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        lines = int(params.get("lines", 100000))
        chunk = ("ERROR flood: connection reset by peer\n")
        with log.open("a", encoding="utf-8") as f:
            for _ in range(lines):
                f.write(chunk)
        ctx["files"].append(str(log))

    elif method == "startup_loop":
        add_mode(target, "startup_loop", ctx)
        kill_service_if_alive(target)

    elif method == "startup_slow":
        add_mode(target, "startup_slow", ctx)
        set_param(target, "hang_sec", int(params.get("hang_sec", 30)))
        kill_service_if_alive(target)

    elif method == "cpu_spike":
        proc = spawn_detached([PY, "-c", "while True: pass"])  # 单核打满
        ctx["pids"].append(proc.pid)

    elif method == "mem_leak":
        code = ("import sys,time; mb=int(sys.argv[1]); buf=[]; total=0\n"
                "while total < mb*1024*1024:\n"
                "    buf.append(b'x'*(1024*1024)); total += 1024*1024\n"
                "time.sleep(86400)")
        proc = spawn_detached([PY, "-c", code, str(int(params.get("mb", 256)))])
        ctx["pids"].append(proc.pid)


# ---------- 恢复 ----------

def apply_recover(method: str, target: str, params: dict, info: dict) -> None:
    """按注入标记恢复单个动作（状态文件统一在 recover 末尾还原）。"""
    if method == "kill_pid":
        if params.get("stale"):
            (RUN_DIR / f"{target}.pid").unlink(missing_ok=True)  # 清掉僵尸 PID 文件

    elif method == "occupy_port":
        for pid in info.get("pids", []):
            kill_by_pid(pid)
        (RUN_DIR / "mock_svc_dup.pid").unlink(missing_ok=True)

    elif method in ("http_5xx", "http_timeout", "health_fail", "cdp_down",
                    "clock_skew", "dep_unavailable", "disk_full_sim",
                    "startup_loop", "startup_slow"):
        pass  # 状态文件在 recover() 末尾按 state_before 还原

    elif method in ("config_missing", "config_malformed"):
        cfg = CONFIG_DIR / f"{target}.conf"
        backup = CONFIG_DIR / f"{target}.conf.bak"
        if backup.exists() and config_is_valid(backup):
            backup.replace(cfg)
        else:  # 备份缺失或也损坏 → 直接恢复干净默认内容，绝不留坏配置
            cfg.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")

    elif method == "db_corrupt":
        run_script(BASE / "mock_db.py", ["--recreate"])

    elif method == "db_lock":
        for pid in info.get("pids", []):
            kill_by_pid(pid)
        run_script(BASE / "mock_db.py", ["--init"])

    elif method == "log_flood":
        (LOG_DIR / f"{target}.log").write_text("", encoding="utf-8")

    elif method in ("cpu_spike", "mem_leak"):
        for pid in info.get("pids", []):
            kill_by_pid(pid)

    elif method == "none":
        pass


# ---------- 子命令 ----------

def cmd_inject(scenario_id: str, dry_run: bool) -> None:
    scenario = get_scenario(scenario_id)
    actions = build_actions(scenario)
    if dry_run:
        print(f"[dry-run] inject {scenario_id}")
        for a in actions:
            print(f"  {a['method']} target={a['target']} params={json.dumps(a['params'], ensure_ascii=False)}")
        print(f"  write marker {marker_path(scenario_id)}")
        return
    if marker_path(scenario_id).exists():
        raise RuntimeError(f"{scenario_id} 已有注入标记，先 recover 再注入")

    ctx = {"pids": [], "files": [], "backups": {}, "state_before": {}}
    for a in actions:
        print(f"[inject] {a['method']} target={a['target']}")
        apply_inject(a["method"], a["target"], a["params"], ctx)

    marker = {
        "scenario_id": scenario_id,
        "injected_at": datetime.now().isoformat(timespec="seconds"),
        "actions": actions,
        "pids": ctx["pids"],
        "files": ctx["files"],
        "backups": ctx["backups"],
        "state_before": ctx["state_before"],
    }
    marker_path(scenario_id).write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[inject] 完成，标记写入 {marker_path(scenario_id)}")


def cmd_recover(scenario_id: str, dry_run: bool) -> None:
    scenario = get_scenario(scenario_id)
    marker_file = marker_path(scenario_id)
    if marker_file.exists():
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        actions = marker["actions"]
        info = {"pids": marker.get("pids", []), "backups": marker.get("backups", {})}
    else:
        actions = build_actions(scenario)
        info = {"pids": [], "backups": {}}
        print(f"[warn] {scenario_id} 无注入标记，按场景定义尽力恢复")

    if dry_run:
        print(f"[dry-run] recover {scenario_id}")
        for a in reversed(actions):
            print(f"  undo {a['method']} target={a['target']}")
        return

    for a in reversed(actions):
        print(f"[recover] undo {a['method']} target={a['target']}")
        apply_recover(a["method"], a["target"], a["params"], info)

    # 状态文件统一还原到注入前
    if marker_file.exists():
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        for name, state in marker.get("state_before", {}).items():
            save_state(name, state)
    # 清理磁盘填充文件（disk_full_sim 可能残留）
    for f in STATE_DIR.glob("disk_fill_*.bin"):
        f.unlink(missing_ok=True)
    marker_file.unlink(missing_ok=True)
    print(f"[recover] {scenario_id} 已恢复，标记已清除")


def cmd_recover_all() -> None:
    markers = sorted(STATE_DIR.glob("injected_*.json"))
    if not markers:
        print("[recover] 无残留注入标记")
        return
    for m in markers:
        sid = m.name[len("injected_"):-len(".json")]
        try:
            cmd_recover(sid, dry_run=False)
        except ValueError as e:
            print(f"[recover] 跳过 {sid}: {e}")


def cmd_list() -> None:
    scenarios = load_matrix()
    baseline = sum(1 for s in scenarios if s["inject"]["method"] == "none")
    mixed = sum(1 for s in scenarios if s.get("mixed"))
    single = len(scenarios) - baseline - mixed  # 互斥分类：非基线且非组合
    for s in scenarios:
        flag = "MIXED" if s.get("mixed") else ("baseline" if s["inject"]["method"] == "none" else "single")
        print(f"{s['scenario_id']:<18} {s['name']:<34} {flag:<8} {s['family']}")
    print(f"共 {len(scenarios)} 个场景（单一 {single} / mixed {mixed} / baseline {baseline}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="故障注入器")
    parser.add_argument("--dry-run", action="store_true", help="只打印动作不执行")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="列出所有场景")
    p_inject = sub.add_parser("inject", help="注入场景")
    p_inject.add_argument("scenario_id")
    p_recover = sub.add_parser("recover", help="恢复场景")
    p_recover.add_argument("scenario_id", nargs="?", default=None)
    p_recover.add_argument("--all", action="store_true", help="恢复所有残留注入")

    args = parser.parse_args()
    if args.command == "list":
        cmd_list()
    elif args.command == "inject":
        cmd_inject(args.scenario_id, args.dry_run)
    elif args.command == "recover":
        if args.all:
            cmd_recover_all()
        elif args.scenario_id:
            cmd_recover(args.scenario_id, args.dry_run)
        else:
            parser.error("recover 需要 scenario_id 或 --all")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
