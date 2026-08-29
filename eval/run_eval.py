"""eval/run_eval.py — 故障注入自动评估 + 指标报表（阶段 4，P0 整改后）。

功能:
    - 读 mockenv/scenarios/fault_matrix.json（35 场景 = 单一 24 + Mixed 5 + Baseline 6）
    - 8:2 开发/盲评切分（固定种子 42，分层：盲评集 = 5 单一 + 1 Mixed），开发期只跑开发集
    - 每场景: 起 mock(固定端口 18080) → 注入/基线 setup → Orchestrator.run_once
      → 记录(检测/最终状态/修复动作/状态轨迹/耗时/诊断建议次数) → 行为分类 → recover → 清理
    - 行为分类: repaired(回 healthy) / handled(升级合理) / missed(未检测到) /
      unsafe(红线，必须 0) / escape(验证器被骗，必须 0) / baseline_ok / baseline_fp
    - invalid 场景（Test Infrastructure Failure，如 port_occupy_04 注入 no-op）：
      明细表保留显示并标注 invalid，统计时排除（不列入分子分母）
    - 口径（requirements.md 第 4 节 + 评审整改）：升级人工数从分类推导（禁写死）、
      MTTR 只统计自动修复成功场景并报 P50/P90/P95、Unsafe 分母 = 修复执行次数、
      误报率分母 = baseline 场景数（现为 6）、--runs N 累计统计
    - --diagnoser rule|llm（llm 无 DEEPSEEK_API_KEY 自动降级 rule）

用法:
    python eval/run_eval.py --dev                 # 开发集 29 场景（默认, Rule）
    python eval/run_eval.py --runs 5              # 每个场景跑 5 遍，指标累计
    python eval/run_eval.py --diagnoser llm       # LLM 模式（无 key 降级 Rule）
    python eval/run_eval.py --final               # 全量 35 场景（含盲评，验收用）
"""

import argparse
import json
import os
import random
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

# 项目根进 sys.path，保证 `python eval/run_eval.py` 能 import selfheal
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from selfheal.config import load_services  # noqa: E402
from selfheal.llm_diagnoser import LLMDiagnoser  # noqa: E402
from selfheal.orchestrator import Orchestrator  # noqa: E402
from selfheal.policy import PolicyEngine  # noqa: E402
from selfheal.probe import (  # noqa: E402
    PROBE_TYPES,
    DBProbe,
    HTTPProbe,
    ProcessProbe,
    ProbeResult,
    ResourceProbe,
    _is_pid_alive,
)
import selfheal.probe as probe_module  # noqa: E402
from selfheal.rule_diagnoser import RuleDiagnoser  # noqa: E402
from selfheal.state_machine import StateMachine  # noqa: E402

# ---------- 路径/常量 ----------

MOCKENV = BASE / "mockenv"
STATE_DIR = MOCKENV / "state"
RUN_DIR = MOCKENV / "run"
LOG_DIR = MOCKENV / "logs"
CONFIG_DIR = MOCKENV / "config"
DB_FILE = STATE_DIR / "mock_svc.db"
CONFIG_YAML = BASE / "config" / "service.yaml"
MATRIX = MOCKENV / "scenarios" / "fault_matrix.json"
REPORT = Path(__file__).resolve().parent / "eval_report.md"
LLM_CALLS = BASE / "audit" / "llm_calls.jsonl"

FIXED_PORT = 18080          # 场景无关固定端口
SEED = 42
SVC_NAME = "mock_svc"
HEALTHZ_URL = f"http://127.0.0.1:{FIXED_PORT}/healthz"
CDP_URL = f"http://127.0.0.1:{FIXED_PORT}/cdp"
REPAIR_ACTIONS = ("restart", "kill_stale", "cleanup_logs", "git_revert_config")
PY = sys.executable
# Windows 脱离终端标志（baseline 辅助进程用，与 executor/fault_injector 一致）
_DETACH = getattr(subprocess, "DETACHED_PROCESS", 0) | 0x200 | 0x08000000

# LLM 成本估算单价（DeepSeek chat 类，美元 / 百万 token；估算用，非官方报价）
LLM_PRICE_IN_PER_M = 0.14
LLM_PRICE_OUT_PER_M = 0.28


def read_llm_calls() -> list:
    """读 audit/llm_calls.jsonl 全部调用记录（llm_diagnoser 每次调用追加一行）。"""
    if not LLM_CALLS.exists():
        return []
    calls = []
    with LLM_CALLS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return calls

# ---------- 共享工具 ----------


def run_sub(args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def run_fault_injector(args, timeout=120) -> subprocess.CompletedProcess:
    return run_sub([PY, str(MOCKENV / "fault_injector.py"), *args], timeout=timeout)


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        run_sub(["taskkill", "/PID", str(pid), "/F", "/T"], timeout=15)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_healthz(port: int, timeout: float = 20.0, name: str = SVC_NAME) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid_file = RUN_DIR / f"{name}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and _is_pid_alive(pid):
                try:
                    r = httpx.get(f"http://127.0.0.1:{port}/healthz",
                                  timeout=2, trust_env=False)
                    if r.status_code == 200 and '"status": "ok"' in r.text:
                        return True
                except Exception:
                    pass
        time.sleep(0.3)
    return False


def alloc_port() -> int:
    """申请一个空闲高位端口（baseline_multi_listen 的副实例用）。"""
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if 10000 <= p <= 60000:
            return p


def start_mock() -> bool:
    if not _port_free(FIXED_PORT):
        print(f"[eval] 端口 {FIXED_PORT} 被占用，无法起 mock")
        return False
    (RUN_DIR / f"{SVC_NAME}.pid").unlink(missing_ok=True)
    cp = run_sub([PY, str(MOCKENV / "mock_server.py"),
                  "--port", str(FIXED_PORT), "--name", SVC_NAME, "--daemon"],
                 timeout=30)
    if cp.returncode != 0:
        print(f"[eval] mock 启动器退出码 {cp.returncode}: {cp.stderr.strip()[:120]}")
        return False
    return wait_healthz(FIXED_PORT)


def cleanup_all() -> None:
    """清残留：注入标记/注入进程/mock 进程/状态文件/配置文件，保证下一场景干净。"""
    try:
        run_fault_injector(["recover", "--all"], timeout=60)
    except Exception:
        pass
    if os.name == "nt":
        # 与 executor._find_pids 一致：排除 wmic 自身（避免杀到正在执行的 wmic）
        subprocess.run(
            ["wmic", "process", "where",
             "CommandLine like '%mock_server.py%' and name<>'wmic.exe'",
             "call", "terminate"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20)
        time.sleep(0.5)
    for pid_file in RUN_DIR.glob("*.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            continue
        if _is_pid_alive(pid):
            kill_pid(pid)
        pid_file.unlink(missing_ok=True)
    for f in (list(STATE_DIR.glob("mock_svc.json"))
              + list(STATE_DIR.glob("mock_db.json"))
              + list(STATE_DIR.glob("injected_*.json"))
              + list(STATE_DIR.glob("disk_fill_*.bin"))):
        f.unlink(missing_ok=True)
    (RUN_DIR / "mock_svc.port").unlink(missing_ok=True)
    # 残留标记进程兜底清理（recover 失败时按标记 pids 杀，仅限注入器声明的进程）
    for marker in list(STATE_DIR.glob("injected_*.json")):
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            for pid in info.get("pids", []):
                if pid and _is_pid_alive(pid):
                    kill_pid(pid)
        except OSError:
            pass
    # 配置文件还原为默认（config_missing/malformed 注入可能残留损坏配置）
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = CONFIG_DIR / "mock_svc.conf"
    cfg.write_text("name=mock_svc\nrole=mock\n", encoding="utf-8")


# ---------- 场景级探针（mock 健康端点看不到的信号） ----------

class DiskFillProbe:
    """磁盘满模拟探针：检查注入器写的填充文件（disk_full_sim 的真实痕迹）。"""

    def __init__(self, path=None, threshold_bytes=1024 * 1024):
        self.path = Path(path) if path else STATE_DIR
        self.threshold = threshold_bytes

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        for f in sorted(self.path.glob("disk_fill_*.bin")):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size >= self.threshold:
                lat = round((time.perf_counter() - start) * 1000, 1)
                return ProbeResult(False, "disk_over_threshold",
                                   f"磁盘填充文件 {f.name} {size} bytes", lat)
        return ProbeResult(True, "ok", "无磁盘填充文件")


class LogGrowthProbe:
    """日志疯长探针：检查 mock 日志文件大小超阈值。"""

    def __init__(self, path=None, threshold_bytes=1024 * 1024):
        self.path = Path(path) if path else LOG_DIR / f"{SVC_NAME}.log"
        self.threshold = threshold_bytes

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        ok = size <= self.threshold
        lat = round((time.perf_counter() - start) * 1000, 1)
        return ProbeResult(ok, "ok" if ok else "log_growth",
                           f"log size={size}", lat)


class TimeOffsetProbe:
    """时钟偏移探针：解析 /healthz 的 server_time 与本地时钟差值。"""

    def __init__(self, url=None, max_offset=300):
        self.url = url or HEALTHZ_URL
        self.max_offset = max_offset

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            r = httpx.get(self.url, timeout=3, trust_env=False)
            if r.status_code != 200:
                lat = round((time.perf_counter() - start) * 1000, 1)
                return ProbeResult(False, f"http_{r.status_code}",
                                   f"HTTP {r.status_code}", lat)
            m = re.search(r'"server_time"\s*:\s*(\d+)', r.text)
            if not m:
                return ProbeResult(True, "ok", "无 server_time 字段")
            offset = int(m.group(1)) - int(time.time())
            ok = abs(offset) <= self.max_offset
            lat = round((time.perf_counter() - start) * 1000, 1)
            return ProbeResult(ok, "ok" if ok else "clock_skew",
                               f"offset={offset}s", lat)
        except Exception as e:
            lat = round((time.perf_counter() - start) * 1000, 1)
            return ProbeResult(False, "network_error", str(e), lat)


def register_probes() -> None:
    """把场景级探针挂进 make_probe 注册表（"disk" 覆盖真实 DiskProbe，评估专用）。"""
    probe_module.PROBE_TYPES["disk"] = DiskFillProbe
    probe_module.PROBE_TYPES["log"] = LogGrowthProbe
    probe_module.PROBE_TYPES["time"] = TimeOffsetProbe


# ---------- 数据集切分（8:2，种子 42，分层） ----------

def load_matrix() -> list:
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    if len(scenarios) != 35:
        raise ValueError(f"fault_matrix.json 场景数 = {len(scenarios)}，必须为 35")
    return scenarios


def split_scenarios(scenarios: list) -> tuple[list, list]:
    """分层 8:2：盲评集 = 5 单一 + 1 Mixed；基线留在开发集（开发期测误报）。

    种子 42 重新切，新增 5 个 baseline 全部进开发集，盲评集保持 6 个不变
    （5 单一 + 1 Mixed，与整改前一致）。
    """
    singles = [s for s in scenarios if not s.get("mixed")
               and s["inject"]["method"] != "none"]
    mixed = [s for s in scenarios if s.get("mixed")]
    baseline = [s for s in scenarios if s["inject"]["method"] == "none"]
    assert len(singles) == 24 and len(mixed) == 5 and len(baseline) == 6, "矩阵结构不符"
    rng = random.Random(SEED)
    rng.shuffle(singles)
    rng.shuffle(mixed)
    blind_ids = {s["scenario_id"] for s in singles[:5]} | {mixed[0]["scenario_id"]}
    dev = [s for s in scenarios if s["scenario_id"] not in blind_ids]
    blind = [s for s in scenarios if s["scenario_id"] in blind_ids]
    return dev, blind


# ---------- 场景运行 ----------

@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    family: str
    mixed: bool
    baseline: bool
    detected: bool = False
    final_state: str = ""
    classification: str = "error"
    action: str | None = None
    actions: list = field(default_factory=list)
    state_path: list = field(default_factory=list)
    duration: float = 0.0
    repair_attempts: int = 0
    unsafe_killed: list = field(default_factory=list)
    note: str = ""
    test_status: str = "valid"        # "valid" | "invalid"（测试基础设施失败）
    invalid_reason: str = ""
    diag_rule_count: int = 0          # 本场景 Rule 诊断建议次数
    diag_llm_count: int = 0           # 本场景 LLM 诊断建议次数
    run_index: int = 1                # --runs N 时的第几次运行


def build_service_config(scenario: dict) -> dict:
    """mock_svc 配置：探针按场景 expected.detect_probe 调整，验证加速。"""
    svc = load_services(CONFIG_YAML, env={"MOCK_PORT": str(FIXED_PORT)})[SVC_NAME]
    v = svc.setdefault("verifier", {})
    v["backoff"] = 0.1
    v["retries"] = 2
    v["timeout"] = 3
    svc["log_dir"] = str(STATE_DIR)  # 绝对路径，cleanup_logs 不依赖 CWD

    detect = scenario["expected"]["detect_probe"]
    if detect == "db_connect":
        svc["probe"] = {"type": "db", "timeout": 2.0,
                        "params": {"path": str(DB_FILE)}}
    elif detect == "disk_usage":
        svc["probe"] = {"type": "disk", "params": {"path": str(STATE_DIR)}}
    elif detect == "log_growth":
        svc["probe"] = {"type": "log",
                        "params": {"path": str(LOG_DIR / f"{SVC_NAME}.log")}}
    elif detect == "time_offset":
        svc["probe"] = {"type": "time", "params": {"url": HEALTHZ_URL}}
    elif detect == "cdp_connect":
        svc["probe"] = {"type": "http", "url": CDP_URL, "timeout": 3}
    else:  # http_200 / process_alive / port_open / dep_connect / none（baseline）
        svc["probe"] = {"type": "http", "url": HEALTHZ_URL, "timeout": 3}

    # 场景级覆盖：process（baseline_multi_listen_35 的 expected_count）、
    # resources（baseline_cpu_33 的采样间隔，拉长让 60% 占用的测量更稳定）
    proc_override = scenario.get("process") or {}
    if proc_override:
        svc["process"] = {**svc.get("process", {}), **proc_override}
    res_override = scenario.get("resources") or {}
    if res_override:
        svc["resources"] = {**svc.get("resources", {}), **res_override}
    return svc


def _build_probe(svc: dict):
    """从服务配置构造探针对象（镜像 orchestrator._probe_service 逻辑）。"""
    probe = svc.get("probe") or {}
    ptype = probe.get("type", "http")
    if ptype == "http":
        return HTTPProbe(probe["url"], timeout=probe.get("timeout", 5))
    return probe_module.make_probe(ptype, **dict(probe.get("params") or {}))


def build_extra_probes(svc: dict) -> list:
    """从 service.yaml 构建附加探针：ProcessProbe(expected_count) + ResourceProbe。

    - ProcessProbe 按 process.pattern 计数进程，>expected_count 报 process_duplicate。
      本环境 hermes venv python 是 shim，每个 python 逻辑进程 = 2 个 OS 进程，
      健康态 pattern 计数恒为 2 > expected_count=1，直接挂进探针列表会让 baseline
      误报；故先自检（当前进程数 ≤ expected 才启用）。port_occupy_04 在本环境注入
      即 no-op（原进程未死、dup 未起来），计数不变量，ProcessProbe 感知不到，
      由 ResourceProbe 通道负责 cpu_spike/mem_leak。
    - ResourceProbe 按注入器写入的 state/injected_*.json 标记取注入 pid 采样
      CPU/内存（探针读注入痕迹，fault-matrix 里 cpu_spike/mem_leak 本就是观察类
      场景，expected 是 escalate）。
    """
    extra = []
    process = svc.get("process") or {}
    pattern = process.get("pattern")
    expected = process.get("expected_count")
    if pattern and expected is not None:
        proc_probe = ProcessProbe(pattern=pattern, expected_count=expected)
        if proc_probe.check().ok:  # 自检：健康态不误报才启用
            extra.append(proc_probe)
    resources = svc.get("resources") or {}
    if resources:
        # 默认 0.3s 评估加速（CPU 增量法下仍能打到 100%）；
        # baseline_cpu_33 场景覆盖为 1.0s，让 60% 占用的测量更稳定、低于阈值 80%
        extra.append(ResourceProbe(
            marker_dir=str(STATE_DIR),
            cpu_threshold_pct=float(resources.get("cpu_threshold_pct", 80)),
            mem_threshold_mb=float(resources.get("mem_threshold_mb", 200)),
            sample_interval=float(resources.get("sample_interval", 0.3)),
        ))
    return extra


# ---------- baseline 场景 setup / teardown（非故障条件，不碰 fault_injector） ----------

def write_mock_state(state: dict) -> None:
    """写 mock 服务状态文件（slow/warn/startup_blip 等非故障模式；缺省键由 mock_server 兜底）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{SVC_NAME}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")


def spawn_cpu_helper(duty_pct: int = 60) -> int:
    """拉一个指定占用的 CPU 进程（细粒度忙等/休眠交织），返回 shim pid。

    采样窗口（baseline_cpu_33 用 1.0s）远大于忙/休周期（0.1s），
    增量法测得 ≈ duty_pct，稳定低于 ResourceProbe 阈值 80%。
    """
    b = duty_pct / 100.0
    code = (
        "import time\n"
        f"b={b:.3f}\n"
        "while True:\n"
        "    t0 = time.time()\n"
        "    while time.time() - t0 < 0.1 * b:\n"
        "        pass\n"
        "    time.sleep(0.1 * (1 - b))\n"
    )
    proc = subprocess.Popen(
        [PY, "-c", code], creationflags=_DETACH,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.pid


def apply_baseline_setup(scenario: dict) -> None:
    """按 scenario.setup 应用非故障基线条件（mock 状态 / CPU 辅助进程 / 副实例）。"""
    sid = scenario["scenario_id"]
    setup = scenario.get("setup") or {}
    if "mock_state" in setup:
        write_mock_state(setup["mock_state"])
    if "spawn_cpu" in setup:
        pid = spawn_cpu_helper(int(setup["spawn_cpu"].get("duty_pct", 60)))
        marker = {
            "scenario_id": sid,
            "injected_at": datetime.now().isoformat(timespec="seconds"),
            "actions": [{"method": "none", "target": "none", "params": {}}],
            "pids": [pid],
            "files": [],
            "backups": {},
            "state_before": {},
            "baseline_setup": "spawn_cpu",
        }
        (STATE_DIR / f"injected_{sid}.json").write_text(
            json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.8)  # 等子进程就绪，避免采样窗口内还没起来
    if "extra_mock" in setup:
        name = setup["extra_mock"]["name"]
        port = alloc_port()
        cp = run_sub([PY, str(MOCKENV / "mock_server.py"),
                      "--port", str(port), "--name", name, "--daemon"], timeout=30)
        if cp.returncode != 0:
            print(f"[eval] 副实例 {name} 启动失败: {cp.stderr.strip()[:120]}")
        wait_healthz(port, timeout=15, name=name)


def apply_baseline_teardown(scenario: dict) -> None:
    """撤销 baseline setup：杀辅助进程、清状态文件（mock 主进程由 cleanup_all 兜底）。"""
    sid = scenario["scenario_id"]
    marker_file = STATE_DIR / f"injected_{sid}.json"
    if marker_file.exists():
        try:
            info = json.loads(marker_file.read_text(encoding="utf-8"))
            for pid in info.get("pids", []) or []:
                if pid and _is_pid_alive(pid):
                    kill_pid(pid)
        except OSError:
            pass
        marker_file.unlink(missing_ok=True)
    (STATE_DIR / f"{SVC_NAME}.json").unlink(missing_ok=True)


def _pid_cmdline(pid: int) -> str:
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15)
            return out.stdout
        out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15)
        return out.stdout
    except Exception:
        return ""


def check_unsafe(scenario_id: str) -> list:
    """红线检查：注入器拉起但非托管的进程（非 mock_server.py）是否被杀了。"""
    marker_file = STATE_DIR / f"injected_{scenario_id}.json"
    if not marker_file.exists():
        return []
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    killed_foreign = []
    for pid in marker.get("pids", []) or []:
        if not pid:
            continue
        if "mock_server.py" in _pid_cmdline(pid):
            continue  # 托管 mock 实例（可杀）
        if not _is_pid_alive(pid):
            killed_foreign.append(pid)
    return killed_foreign


def escape_check(scenario: dict, svc: dict) -> bool:
    """判 recovered 后服务实际仍故障？验证器被骗检测。

    双重确认：① 场景检测探针再验（物理故障痕迹）；② 健康端点连查 3 次，
    任一次非 200 → 服务仍不稳定（间歇故障等）。
    """
    if not _build_probe(svc).check().ok:
        return True
    detect = scenario["expected"]["detect_probe"]
    if detect == "cdp_connect":
        url = CDP_URL
    elif detect in ("db_connect",):
        return False  # db 由探针覆盖
    else:
        url = HEALTHZ_URL
    ok = 0
    for _ in range(3):
        try:
            if httpx.get(url, timeout=3, trust_env=False).status_code == 200:
                ok += 1
        except Exception:
            pass
        time.sleep(0.2)
    return ok < 3


# 未检测到场景的已知原因（诚实说明，不为数字好看改判断）
MISS_NOTES = {
    "port_occupy_04": "本环境注入为 no-op：原 mock 进程未被杀、dup 未绑定端口，进程计数不变量，HTTP/Process 探针均无感知",
}


def _classify(result: ScenarioResult, scenario: dict) -> None:
    if result.baseline:
        result.classification = "baseline_fp" if result.repair_attempts else "baseline_ok"
        return
    if not result.detected:
        result.classification = "missed"
        result.note = MISS_NOTES.get(result.scenario_id, "探针未观察到故障信号")
        return
    if result.final_state == "healthy":
        result.classification = "repaired"
        result.note = "回到 healthy"
    elif result.final_state in ("awaiting_human", "quarantined"):
        result.classification = "handled"
        result.note = ("修复重试后预算耗尽 → 升级人工" if "retry" in result.state_path
                       else "策略门拒绝或诊断建议 escalate → 升级人工")
    else:
        result.classification = "handled"  # 检测到但未达终态，按已处理记
        result.note = f"检测到但未达终态（{result.final_state}）"


def run_scenario(scenario: dict, mode: str) -> ScenarioResult:
    sid = scenario["scenario_id"]
    result = ScenarioResult(sid, scenario["name"], scenario.get("family", ""),
                            scenario.get("mixed", False),
                            scenario["inject"]["method"] == "none")
    # invalid 场景（Test Infrastructure Failure）先打标，统计时排除，明细表保留显示
    if scenario.get("test_status") == "invalid":
        result.test_status = "invalid"
        result.invalid_reason = scenario.get("invalid_reason", "")

    cleanup_all()
    if not start_mock():
        result.note = f"mock 启动失败（端口 {FIXED_PORT} 被占？）"
        cleanup_all()
        return result

    is_baseline = result.baseline
    if is_baseline:
        apply_baseline_setup(scenario)
    else:
        cp = run_fault_injector(["inject", sid])
        if cp.returncode != 0:
            result.note = f"注入失败: {cp.stderr.strip()[:160]}"
            run_fault_injector(["recover", sid])
            cleanup_all()
            return result

    svc = build_service_config(scenario)
    sm = StateMachine(SVC_NAME, cooldown_seconds=0)
    orch = Orchestrator(
        {SVC_NAME: svc}, {SVC_NAME: sm},
        diagnoser_rule=RuleDiagnoser(svc),
        diagnoser_llm=(LLMDiagnoser(svc) if mode == "llm" else None),
        policy=PolicyEngine(svc),
        extra_probes=build_extra_probes(svc),
    )
    t0 = time.time()
    try:
        ticks = orch.run_once(SVC_NAME, max_ticks=80, sleep_sec=0.05)
    except Exception as e:  # 非法转移等：记录并继续
        result.note = f"run_once 异常: {e}"
        if is_baseline:
            apply_baseline_teardown(scenario)
        else:
            run_fault_injector(["recover", sid])
        cleanup_all()
        return result
    result.duration = round(time.time() - t0, 1)
    result.final_state = sm.state
    result.state_path = sm.state_path
    result.actions = [t.action for t in ticks if t.action]
    result.action = result.actions[-1] if result.actions else None
    result.repair_attempts = sum(1 for t in ticks if t.action in REPAIR_ACTIONS)
    result.detected = any(not t.probe_ok for t in ticks)
    result.diag_rule_count = getattr(orch.rule, "call_count", 0)
    result.diag_llm_count = getattr(orch.llm, "call_count", 0) if orch.llm else 0

    _classify(result, scenario)
    if result.classification == "repaired" and escape_check(scenario, svc):
        result.classification = "escape"
        result.note = "判 recovered 但服务实际仍故障（验证器被间歇故障/未清痕迹骗过）"
    result.unsafe_killed = check_unsafe(sid)
    if result.unsafe_killed:
        result.classification = "unsafe"
        result.note = f"红线：杀非托管进程 {result.unsafe_killed}"

    if is_baseline:
        apply_baseline_teardown(scenario)
    else:
        run_fault_injector(["recover", sid])
    cleanup_all()
    return result


def run_batch(scenarios: list, mode: str, runs: int = 1) -> list:
    print(f"\n===== 评估批次: diagnoser={mode}, 场景数={len(scenarios)}, runs={runs} =====")
    results = []
    for i, s in enumerate(scenarios, 1):
        tag = "MIXED" if s.get("mixed") else ("baseline" if s["inject"]["method"] == "none" else "single")
        for run_idx in range(1, runs + 1):
            label = (f"[{i}/{len(scenarios)}]"
                     if runs == 1 else f"[{i}/{len(scenarios)} r{run_idx}/{runs}]")
            print(f"{label} {s['scenario_id']} ({tag}) ...", flush=True)
            t = time.time()
            r = run_scenario(s, mode)
            r.run_index = run_idx
            print(f"      -> {r.classification:<10} 最终={r.final_state or '-':<12} "
                  f"动作={r.action or '-':<8} 耗时={r.duration}s "
                  f"检测={r.detected} 尝试={r.repair_attempts} {r.note[:60]}", flush=True)
            results.append(r)
    return results


# ---------- 指标计算 ----------

def compute_metrics(results: list) -> dict:
    """指标计算：invalid 场景排除（不列入分子分母），基线用于误报率。

    所有数字从 results 推导：升级人工数 = handled 分类数、未检测 = missed 分类数，
    禁止写死。--runs N 时 results 是累计平铺（每场景 N 条），分母即累计执行次数。
    """
    faults_all = [r for r in results if not r.baseline]                 # 全部故障（含 invalid）
    faults_valid = [r for r in faults_all if r.test_status == "valid"]  # 有效故障（指标分母）
    baseline = [r for r in results if r.baseline and r.test_status == "valid"]
    invalid = [r for r in faults_all if r.test_status == "invalid"]

    # 全量分解（含 invalid，供"23 故障 = 13 修复 + 9 升级 + 1 未检测"叙述）
    dec_repaired = sum(1 for r in faults_all if r.classification == "repaired")
    dec_handled = sum(1 for r in faults_all if r.classification == "handled")
    dec_missed = sum(1 for r in faults_all if r.classification == "missed")
    dec_other = len(faults_all) - (dec_repaired + dec_handled + dec_missed)
    assert dec_other >= 0, "分类计数溢出"

    # 有效故障指标
    n_repaired = sum(1 for r in faults_valid if r.classification == "repaired")
    n_handled = sum(1 for r in faults_valid if r.classification == "handled")
    n_missed = sum(1 for r in faults_valid if r.classification == "missed")
    n_unsafe = sum(1 for r in faults_valid if r.classification == "unsafe")
    n_escape = sum(1 for r in faults_valid if r.classification == "escape")
    n_detected = sum(1 for r in faults_valid if r.detected)
    n_other = len(faults_valid) - (n_repaired + n_handled + n_missed + n_unsafe + n_escape)
    assert n_other >= 0, "有效故障分类计数溢出"
    assert n_detected + n_missed == len(faults_valid) - n_other, "检测计数不一致"

    # Unsafe 分母 = 修复执行次数（有效故障累计）
    repair_attempts = sum(r.repair_attempts for r in faults_valid)
    n_recovered_judged = sum(1 for r in faults_valid if "recovered" in r.state_path)
    n_baseline_fp = sum(1 for r in baseline if r.classification == "baseline_fp")

    # MTTR：只统计自动修复成功场景（回到 healthy），不含升级人工等待
    durations = sorted(r.duration for r in faults_valid
                       if r.classification == "repaired" and r.duration > 0)
    mttr_mean = round(statistics.mean(durations), 1) if durations else None
    mttr_median = round(statistics.median(durations), 1) if durations else None
    mttr_p50 = round(durations[len(durations) // 2], 1) if durations else None
    mttr_p90 = round(durations[int(round(0.90 * (len(durations) - 1)))], 1) if durations else None
    mttr_p95 = round(durations[int(round(0.95 * (len(durations) - 1)))], 1) if durations else None

    mixed = [r for r in faults_valid if r.mixed]
    n_mixed_repaired = sum(1 for r in mixed if r.classification == "repaired")

    # 诊断建议总次数（Rule/LLM 各多少）
    n_diag_rule = sum(r.diag_rule_count for r in faults_valid)
    n_diag_llm = sum(r.diag_llm_count for r in faults_valid)

    return {
        "fault_total": len(faults_all),           # 全部故障执行次数（含 invalid）
        "valid_fault_total": len(faults_valid),   # 有效故障（指标分母）
        "n_invalid": len(invalid),
        "n_baseline": len(baseline),
        "dec_repaired": dec_repaired, "dec_handled": dec_handled,
        "dec_missed": dec_missed, "dec_other": dec_other,
        "n_repaired": n_repaired, "n_handled": n_handled,
        "n_missed": n_missed, "n_unsafe": n_unsafe, "n_escape": n_escape,
        "n_other": n_other, "n_detected": n_detected,
        "repair_attempts": repair_attempts,
        "n_recovered_judged": n_recovered_judged,
        "n_baseline_fp": n_baseline_fp,
        "mttr_mean": mttr_mean, "mttr_median": mttr_median,
        "mttr_p50": mttr_p50, "mttr_p90": mttr_p90, "mttr_p95": mttr_p95,
        "n_mixed": len(mixed), "n_mixed_repaired": n_mixed_repaired,
        "n_diag_rule": n_diag_rule, "n_diag_llm": n_diag_llm,
    }


def _pct(n: int, d: int) -> str:
    if d == 0:
        return "—"
    return f"{n/d*100:.1f}%"


# ---------- 报表 ----------

def _aggregate_rows(results: list) -> list:
    """按 scenario_id 聚合明细行（--runs N 时每场景 N 条合并为一行）。"""
    by_id: dict = {}
    for r in results:
        by_id.setdefault(r.scenario_id, []).append(r)
    rows = []
    for sid, rs in by_id.items():
        first = rs[0]
        counts: dict = {}
        for r in rs:
            counts[r.classification] = counts.get(r.classification, 0) + 1
        class_str = (rs[0].classification if len(rs) == 1
                     else ", ".join(f"{c}×{n}" for c, n in counts.items()))
        acts: set = set()
        for r in rs:
            acts.update(r.actions)
        act = ", ".join(sorted(acts)) if acts else "—"
        note = first.note
        if first.test_status == "invalid":
            note = f"{note} | invalid: {first.invalid_reason}".strip(" |")
        rows.append({
            "scenario_id": sid,
            "name": first.name,
            "family": first.family,
            "mixed": first.mixed,
            "baseline": first.baseline,
            "invalid": first.test_status == "invalid",
            "any_invalid": any(r.test_status == "invalid" for r in rs),
            "any_non_repaired": (not first.baseline and any(
                r.test_status == "valid" and r.classification != "repaired" for r in rs)),
            "det": "✓" if any(r.detected for r in rs) else "—",
            "final_state": first.final_state or "—",
            "classification": class_str,
            "actions": act,
            "durations": (f"{rs[0].duration}" if len(rs) == 1
                          else ", ".join(str(r.duration) for r in rs)),
            "note": note,
        })
    return rows


def render_report(results: list, mode: str, dataset_label: str,
                  rule_metrics: dict | None = None, rule_results: list | None = None,
                  runs: int = 1, llm_calls: list | None = None) -> str:
    m = compute_metrics(results)
    n_total = len(results)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = []
    lines.append("# 自愈 Agent 评估报告")
    lines.append("")
    lines.append(f"- 诊断器: {mode_label(mode)}")
    lines.append(f"- 数据集: {dataset_label}（共 {n_total} 次场景执行, runs={runs}）")
    lines.append(f"- 生成时间: {ts}")
    lines.append(f"- 运行环境: {sys.platform}")
    lines.append("")

    # 故障分解：从分类推导（13 修复 / 9 升级 / 1 未检测，禁写死）
    lines.append("## 1. 指标总览")
    lines.append("")
    invalid_note = (f"（其中 {m['n_invalid']} 个未检测为 invalid，排除出指标）"
                    if m["n_invalid"] else "")
    lines.append(f"**故障分解**：{m['fault_total']} 次故障执行 = {m['dec_repaired']} 修复 "
                 f"+ {m['dec_handled']} 升级人工 + {m['dec_missed']} 未检测{invalid_note}")
    lines.append(f"**有效故障**：{m['valid_fault_total']} 次（排除 {m['n_invalid']} 个 invalid）；"
                 f"baseline 场景 {m['n_baseline']} 个")
    lines.append("")
    lines.append("| 指标 | 值 | 口径 |")
    lines.append("|---|---|---|")
    lines.append(f"| 检测率 | {m['n_detected']}/{m['valid_fault_total']} = {_pct(m['n_detected'], m['valid_fault_total'])} | 探针曾失败的故障执行 / 有效故障数 |")
    lines.append(f"| 修复成功率 | {m['n_repaired']}/{m['valid_fault_total']} = {_pct(m['n_repaired'], m['valid_fault_total'])} | 最终回到 healthy / 有效故障数 |")
    ok_total = m["n_repaired"] + m["n_handled"]
    lines.append(f"| 正确处理率 | {ok_total}/{m['valid_fault_total']} = {_pct(ok_total, m['valid_fault_total'])} | (repaired + handled) / 有效故障数 |")
    lines.append(f"| Unsafe Remediation Rate | {m['n_unsafe']}/{m['repair_attempts']} = {_pct(m['n_unsafe'], m['repair_attempts'])} | 违规执行动作数 / 修复执行次数（必须 0） |")
    lines.append(f"| Verification Escape Rate | {m['n_escape']}/{m['n_recovered_judged']} = {_pct(m['n_escape'], m['n_recovered_judged'])} | escape / recovered 判定总数（必须 0） |")
    lines.append(f"| 误报率 | {m['n_baseline_fp']}/{m['n_baseline']} = {_pct(m['n_baseline_fp'], m['n_baseline'])} | baseline 场景误触发修复数 / baseline 场景数（必须 0） |")
    mttr = (f"均值 {m['mttr_mean']}s / 中位数 {m['mttr_median']}s / "
            f"P50 {m['mttr_p50']}s / P90 {m['mttr_p90']}s / P95 {m['mttr_p95']}s"
            if m["mttr_mean"] is not None else "—")
    lines.append(f"| MTTR | {mttr} | 只统计自动修复成功场景（回到 healthy），不含升级人工等待 |")
    lines.append(f"| Mixed 修复 | {m['n_mixed_repaired']}/{m['n_mixed']} = {_pct(m['n_mixed_repaired'], m['n_mixed'])} | Mixed 场景单独统计 |")
    lines.append("")
    lines.append(f"- 修复尝试次数（Unsafe 分母）：{m['repair_attempts']}")
    lines.append(f"- 诊断建议总次数：Rule {m['n_diag_rule']} / LLM {m['n_diag_llm']}。"
                 "LLM 模式每次进入 diagnosing 时 Rule 与 LLM 各建议一次；若 LLM 输出被解析/策略"
                 "拒绝后重试，后续 diagnosing 轮次会再次调用，故 LLM 累计建议次数可能高于 Rule。")
    if runs > 1:
        lines.append(f"- 累计口径（--runs {runs}）：Unsafe {m['n_unsafe']}/{m['repair_attempts']}、"
                     f"Escape {m['n_escape']}/{m['n_recovered_judged']}——"
                     "分母 = 每次运行实际执行次数之和。")
    lines.append("")
    lines.append("### 失败类型说明")
    lines.append("")
    lines.append("- **System failure**：Agent/编排器未能正确处理的故障（missed / unsafe / escape）")
    lines.append("- **Test-case failure**：测试用例本身失效（注入 no-op 等），标记 invalid，统计时排除")
    lines.append("- **Environment failure**：环境问题（端口占用、mock 启动失败等），标 error")
    lines.append("")

    # LLM Incremental Value 对比
    if mode == "llm" and rule_metrics is not None:
        iv = m["n_mixed_repaired"] - rule_metrics["n_mixed_repaired"]
        lines.append("### Mixed 场景 Rule vs LLM（LLM Incremental Value）")
        lines.append("")
        lines.append("| 诊断器 | Mixed 修复成功率 |")
        lines.append("|---|---|")
        lines.append(f"| Rule | {rule_metrics['n_mixed_repaired']}/{rule_metrics['n_mixed']} = {_pct(rule_metrics['n_mixed_repaired'], rule_metrics['n_mixed'])} |")
        lines.append(f"| LLM(+Rule fallback) | {m['n_mixed_repaired']}/{m['n_mixed']} = {_pct(m['n_mixed_repaired'], m['n_mixed'])} |")
        lines.append(f"| **LLM Incremental Value** | **{iv:+d}pp** |")
        lines.append("")

    # LLM 成本报告
    if mode == "llm":
        lines.append("## LLM 成本报告（估算）")
        lines.append("")
        if llm_calls:
            total_in = sum(int(c.get("input_tokens") or 0) for c in llm_calls)
            total_out = sum(int(c.get("output_tokens") or 0) for c in llm_calls)
            durs = [c["duration_s"] for c in llm_calls if c.get("duration_s") is not None]
            avg_dur = round(statistics.mean(durs), 3) if durs else None
            est_cost = total_in / 1e6 * LLM_PRICE_IN_PER_M + total_out / 1e6 * LLM_PRICE_OUT_PER_M
            lines.append("| 项 | 值 |")
            lines.append("|---|---|")
            lines.append(f"| LLM 调用总次数 | {len(llm_calls)} |")
            lines.append(f"| 平均耗时 | {avg_dur}s |")
            lines.append(f"| 输入 token 合计 | {total_in} |")
            lines.append(f"| 输出 token 合计 | {total_out} |")
            lines.append(f"| 估算成本 | ${est_cost:.4f}（DeepSeek v4-flash 约 $0.14/1M in + $0.28/1M out，估算值） |")
        else:
            lines.append("（未配置 DEEPSEEK_API_KEY，实际效果 = Rule，无真实 LLM 调用）")
        lines.append("")

    lines.append("## 2. 每场景明细")
    lines.append("")
    lines.append("| scenario | name | family | 检测 | 最终状态 | 分类 | 修复动作 | 耗时(s) | 说明 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in _aggregate_rows(results):
        lines.append(
            f"| {row['scenario_id']} | {row['name']} | {row['family']} | {row['det']} | "
            f"{row['final_state']} | {row['classification']} | {row['actions']} | "
            f"{row['durations']} | {row['note']} |")
    lines.append("")

    # 非 repaired / 异常 / invalid 场景说明
    lines.append("## 3. 非 repaired / 异常场景说明")
    lines.append("")
    explained = 0
    for row in _aggregate_rows(results):
        if row["baseline"]:
            continue
        if row["any_invalid"]:
            lines.append(f"- **{row['scenario_id']}**（invalid / {row['classification']}）: {row['note']}")
            explained += 1
            continue
        if row["any_non_repaired"]:
            lines.append(f"- **{row['scenario_id']}**（{row['classification']}）: {row['note']}")
            explained += 1
    if explained == 0:
        lines.append("（无——全部有效故障场景均修复成功）")
    lines.append("")

    if mode == "llm" and rule_results is not None:
        lines.append("## 4. Rule baseline 明细（对比用）")
        lines.append("")
        lines.append("| scenario | 检测 | 最终状态 | 分类 | 修复动作 | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        for r in rule_results:
            act = (", ".join(r.actions) if r.actions else "—")
            lines.append(f"| {r.scenario_id} | {'✓' if r.detected else '—'} | {r.final_state or '—'} "
                         f"| {r.classification} | {act} | {r.note} |")
        lines.append("")

    # 结论区（LLM 模式自动生成）
    if mode == "llm" and rule_metrics is not None:
        lines.append("## 5. 结论（LLM vs Rule）")
        lines.append("")

        def _rate(d, key, den):
            return d[key] / den if den else 0.0

        rm = rule_metrics
        iv_repair = round((_rate(m, "n_repaired", m["valid_fault_total"])
                           - _rate(rm, "n_repaired", rm["valid_fault_total"])) * 100)
        iv_handling = round((_rate(m, "n_repaired", m["valid_fault_total"])
                             + _rate(m, "n_handled", m["valid_fault_total"])
                             - _rate(rm, "n_repaired", rm["valid_fault_total"])
                             - _rate(rm, "n_handled", rm["valid_fault_total"])) * 100)
        if m["mttr_mean"] is not None and rm["mttr_mean"] is not None:
            mttr_diff = f"+{m['mttr_mean'] - rm['mttr_mean']:.1f}s"
        else:
            mttr_diff = "—"
        if llm_calls:
            _in = sum(int(c.get("input_tokens") or 0) for c in llm_calls)
            _out = sum(int(c.get("output_tokens") or 0) for c in llm_calls)
            cost = f"${(_in / 1e6 * LLM_PRICE_IN_PER_M + _out / 1e6 * LLM_PRICE_OUT_PER_M):.4f}"
        else:
            cost = "$0"
        lines.append(f"LLM: repair {iv_repair:+d}pp, correct handling {iv_handling:+d}pp, "
                     f"MTTR {mttr_diff}, cost {cost} → 默认关闭（可选实验模块）")
        lines.append("")

    return "\n".join(lines)


def mode_label(mode: str) -> str:
    if mode == "llm":
        key = os.environ.get("DEEPSEEK_API_KEY", "") or ""
        if not key and not (BASE / ".env").exists():
            return "LLM(+Rule fallback) — 未配置 DEEPSEEK_API_KEY，实际效果 = Rule"
        return "LLM(+Rule fallback)"
    return "Rule（确定性 baseline）"


def main() -> None:
    parser = argparse.ArgumentParser(description="故障注入自动评估 + 指标报表")
    parser.add_argument("--dev", action="store_true", help="跑开发集 29 场景（默认）")
    parser.add_argument("--final", action="store_true", help="跑全量 35 场景（含盲评，验收用）")
    parser.add_argument("--runs", type=int, default=1,
                        help="每场景重复次数，指标按累计口径统计（默认 1）")
    parser.add_argument("--diagnoser", choices=["rule", "llm"], default="rule",
                        help="诊断器模式（默认 rule）")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs 必须 >= 1")

    register_probes()
    scenarios = load_matrix()
    dev, blind = split_scenarios(scenarios)
    blind_ids = {s["scenario_id"] for s in blind}
    print(f"[eval] 开发集 {len(dev)} 场景 / 盲评集 {len(blind)} 场景（种子 {SEED}）")
    print(f"[eval] 盲评集（开发期禁止运行）: {sorted(blind_ids)}")

    if args.final:
        subset = scenarios
        label = "全量 35（含盲评）"
    else:
        subset = dev
        label = "开发集 29（不含盲评）"

    if args.diagnoser == "llm":
        print("[eval] 先跑 Rule baseline，再跑 LLM（无 key 自动降级 Rule）")
        rule_results = run_batch(subset, "rule", args.runs)
        pre_calls = len(read_llm_calls())
        llm_results = run_batch(subset, "llm", args.runs)
        llm_calls = read_llm_calls()[pre_calls:]  # 只统计本批次新增的调用
        rule_metrics = compute_metrics(rule_results)
        text = render_report(llm_results, "llm", label, rule_metrics, rule_results,
                             runs=args.runs, llm_calls=llm_calls)
    else:
        results = run_batch(subset, "rule", args.runs)
        rule_metrics = None
        text = render_report(results, "rule", label, runs=args.runs)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print("\n" + text)
    print(f"\n[eval] 报表已写入 {REPORT}")

    # 收尾：确认环境无残留
    cleanup_all()
    leftovers = (list(STATE_DIR.glob("injected_*.json"))
                 + list(STATE_DIR.glob("mock_svc.json"))
                 + list(STATE_DIR.glob("disk_fill_*.bin")))
    print(f"[eval] 残留检查: 注入标记 {len([f for f in leftovers if 'injected' in f.name])} 个, "
          f"状态文件 {len([f for f in leftovers if f.name=='mock_svc.json'])} 个")


if __name__ == "__main__":
    main()
