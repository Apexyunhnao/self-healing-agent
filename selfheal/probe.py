"""selfheal/probe.py — 确定性探针。

感知服务状态：HTTP / 进程 / DB / 磁盘 / 资源占用。全部确定性，禁止调用 LLM。
统一接口 check() -> ProbeResult。
所有 subprocess.run 带 encoding="utf-8", errors="replace"。
所有 httpx 请求 trust_env=False（Windows 系统代理 V2RayN 会拦截 localhost）。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

_CMD_TIMEOUT = 10


@dataclass
class ProbeResult:
    ok: bool
    status: str = ""
    detail: str = ""
    latency_ms: float = 0.0


def _is_pid_alive(pid: int) -> bool:
    """Windows: tasklist 按 PID 查；POSIX: os.kill(pid, 0)。"""
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_CMD_TIMEOUT,
        )
        return f'"{pid}"' in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _tasklist_has_image(image: str) -> bool:
    """Windows: tasklist 按映像名（含 .exe）查进程是否存在。"""
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_CMD_TIMEOUT,
    )
    # CSV 首列是带引号的映像名（可能含逗号，取整行包含即可）
    return image.lower() in out.stdout.lower()


def _find_pids(pattern: str) -> list:
    """按命令行 pattern 找进程 PID。

    Windows 用 wmic 匹配 CommandLine；排除 wmic 自身（否则自匹配）。
    POSIX 用 pgrep -f；排除当前进程及父进程。
    """
    if os.name == "nt":
        query = f"CommandLine like '%{pattern}%' and name<>'wmic.exe'"
        out = subprocess.run(
            ["wmic", "process", "where", query, "get", "ProcessId"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_CMD_TIMEOUT,
        )
        pids = [int(line.strip()) for line in out.stdout.splitlines()
                if line.strip().isdigit()]
        return pids

    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=_CMD_TIMEOUT)
    me = {os.getpid(), os.getppid()}
    return [int(line.strip()) for line in out.stdout.splitlines()
            if line.strip().isdigit() and int(line.strip()) not in me]


class HTTPProbe:
    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    def check(self) -> ProbeResult:
        if httpx is None:
            return ProbeResult(False, "no_httpx", "httpx 未安装")
        start = time.perf_counter()
        try:
            r = httpx.get(self.url, timeout=self.timeout, trust_env=False)
            latency_ms = (time.perf_counter() - start) * 1000
            ok = r.status_code == 200
            detail = f"HTTP {r.status_code} {r.text.strip()[:120]}"
            return ProbeResult(ok, f"http_{r.status_code}" if not ok else "ok",
                               detail, round(latency_ms, 1))
        except Exception as e:  # 连接失败/超时/DNS 等
            latency_ms = (time.perf_counter() - start) * 1000
            return ProbeResult(False, "network_error", str(e), round(latency_ms, 1))


class ProcessProbe:
    """按 PID 文件 / 进程名 / 命令行 pattern 探测进程。

    name 为 tasklist 映像名（如 python.exe）；pid_file 优先于 name；
    pattern 为命令行匹配（如 "mock_server.py"），配合 expected_count 检测
    重复实例：进程数 > expected_count → process_duplicate；进程数 == 0 → process_missing。
    """

    def __init__(self, name: str | None = None, pid_file: str | None = None,
                 pattern: str | None = None, expected_count: int | None = None):
        self.name = name
        self.pid_file = Path(pid_file) if pid_file else None
        self.pattern = pattern
        self.expected_count = expected_count

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        lat = lambda: round((time.perf_counter() - start) * 1000, 1)
        if self.pid_file is not None:
            if not self.pid_file.exists():
                return ProbeResult(False, "no_pid_file",
                                   f"PID 文件缺失: {self.pid_file}", lat())
            try:
                pid = int(self.pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                return ProbeResult(False, "bad_pid_file",
                                   f"PID 文件内容非法: {self.pid_file}", lat())
            alive = _is_pid_alive(pid)
            return ProbeResult(alive, "alive" if alive else "dead",
                               f"pid={pid}", lat())
        if self.pattern:
            pids = _find_pids(self.pattern)
            count = len(pids)
            if self.expected_count is not None:
                if count == 0:
                    return ProbeResult(False, "process_missing",
                                       f"pattern={self.pattern} 无匹配进程", lat())
                if count > self.expected_count:
                    return ProbeResult(False, "process_duplicate",
                                       f"pattern={self.pattern} 进程数 {count} > expected {self.expected_count}",
                                       lat())
            alive = count > 0
            return ProbeResult(alive, "alive" if alive else "dead",
                               f"pattern={self.pattern} count={count}", lat())
        if self.name:
            alive = _tasklist_has_image(self.name)
            return ProbeResult(alive, "alive" if alive else "dead",
                               f"image={self.name}", lat())
        return ProbeResult(False, "no_criteria", "未提供 name/pid_file/pattern", lat())


class DBProbe:
    def __init__(self, path: str, timeout: float = 2.0):
        self.path = Path(path)
        self.timeout = timeout

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        if not self.path.exists():
            return ProbeResult(False, "db_missing", f"DB 文件不存在: {self.path}", 0.0)
        try:
            conn = sqlite3.connect(str(self.path), timeout=self.timeout)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
            integrity = row[0] if row else ""
            ok = integrity == "ok"
            latency = round((time.perf_counter() - start) * 1000, 1)
            return ProbeResult(ok, "ok" if ok else "db_integrity",
                               f"integrity={integrity!r}", latency)
        except sqlite3.Error as e:
            latency = round((time.perf_counter() - start) * 1000, 1)
            return ProbeResult(False, "db_error", f"{type(e).__name__}: {e}", latency)


class DiskProbe:
    def __init__(self, path: str, threshold_pct: float = 90.0):
        self.path = Path(path)
        self.threshold_pct = threshold_pct

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            usage = shutil.disk_usage(self.path)
        except OSError as e:
            return ProbeResult(False, "disk_error", str(e), 0.0)
        used_pct = (1 - usage.free / usage.total) * 100
        ok = used_pct <= self.threshold_pct
        latency = round((time.perf_counter() - start) * 1000, 1)
        detail = (f"used={used_pct:.1f}% (threshold {self.threshold_pct}%) "
                  f"total={usage.total // (1024 ** 3)}GB")
        return ProbeResult(ok, "ok" if ok else "disk_over_threshold",
                           detail, latency)


class ResourceProbe:
    """资源占用探针：监控目标进程 CPU / 内存，超阈值判故障。

    目标解析优先级：marker_dir（读 mockenv/state/injected_*.json 里的注入 pid，
    评估场景专用）> pid > process_name（命令行 pattern）。
    CPU 用两次采样差值（sample_interval 秒间隔），内存取当前工作集
    （psutil 优先，wmic 兜底，后者 KernelModeTime/UserModeTime 单位 100ns）。
    显式目标找不到进程 → process_missing；marker 模式无注入进程 → 正常（不误报）。
    """

    def __init__(self, process_name: str | None = None, pid: int | None = None,
                 marker_dir: str | None = None,
                 cpu_threshold_pct: float = 80.0,
                 mem_threshold_mb: float = 200.0,
                 sample_interval: float = 1.0):
        self.process_name = process_name
        self.pid = pid
        self.marker_dir = Path(marker_dir) if marker_dir else None
        self.cpu_threshold = cpu_threshold_pct
        self.mem_threshold_mb = mem_threshold_mb
        self.sample_interval = sample_interval

    # ---- 目标解析 ----

    def _target_pids(self) -> list | None:
        """返回待监控 pid 列表；None 表示未配置监控目标。"""
        if self.marker_dir is not None:
            pids = []
            for m in sorted(self.marker_dir.glob("injected_*.json")):
                try:
                    info = json.loads(m.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for p in info.get("pids", []) or []:
                    if p and p not in pids:
                        pids.append(p)
            return pids  # 空列表 = 无注入资源进程，视为正常
        if self.pid is not None:
            return [self.pid]
        if self.process_name:
            return _find_pids(self.process_name)
        return None

    def _expand_descendants(self, pids: list) -> list:
        """把每个目标进程的子孙进程并入监控列表。

        本环境 python.exe 是 shim（父进程仅等待 uv 子进程，自身不吃 CPU），
        注入器记录的 pid 是 shim，实际 busy-loop/分配内存在子进程里，必须展开。
        """
        try:
            import psutil
        except ImportError:  # pragma: no cover
            return list(pids)
        expanded = list(pids)
        for pid in pids:
            try:
                children = psutil.Process(pid).children(recursive=True)
            except psutil.Error:
                continue
            for c in children:
                if c.pid not in expanded:
                    expanded.append(c.pid)
        return expanded

    def _sample_one(self, pid: int):
        """返回 (cpu_total_seconds, working_set_bytes)；进程不存在返回 None。"""
        try:
            import psutil
        except ImportError:  # pragma: no cover
            return self._sample_wmic(pid)
        try:
            p = psutil.Process(pid)
            t = p.cpu_times()
            w = p.memory_info().rss  # Windows 下即 WorkingSetSize
            return (t.user + t.system, w)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    @staticmethod
    def _sample_wmic(pid: int):
        """wmic 兜底采样：KernelModeTime/UserModeTime 单位 100ns，WorkingSetSize 字节。"""
        try:
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}",
                 "get", "KernelModeTime,UserModeTime,WorkingSetSize", "/format:list"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_CMD_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        vals = {}
        for line in out.stdout.splitlines():
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip()
        if "WorkingSetSize" not in vals:
            return None
        try:
            cpu = (int(vals.get("KernelModeTime", 0)) + int(vals.get("UserModeTime", 0))) * 1e-7
            return (cpu, int(vals["WorkingSetSize"]))
        except ValueError:
            return None

    # ---- 检查 ----

    def check(self) -> ProbeResult:
        start = time.perf_counter()
        pids = self._target_pids()
        if pids is None:
            return ProbeResult(True, "ok", "未配置监控目标", 0.0)
        if not pids:
            if self.marker_dir is not None:
                return ProbeResult(True, "ok", "无注入资源进程", 0.0)
            return ProbeResult(False, "process_missing", "目标进程不存在", 0.0)

        targets = self._expand_descendants(pids)
        s1 = {pid: v for pid in targets if (v := self._sample_one(pid)) is not None}
        time.sleep(self.sample_interval)
        s2 = {pid: v for pid in targets if (v := self._sample_one(pid)) is not None}
        if not s2:
            if self.marker_dir is not None:
                return ProbeResult(True, "ok", "注入进程已退出", 0.0)
            return ProbeResult(False, "process_missing", "目标进程不存在",
                               round((time.perf_counter() - start) * 1000, 1))

        for pid in s2:
            cpu1 = s1.get(pid, (0.0, 0))[0]
            cpu2, mem = s2[pid]
            cpu_pct = (cpu2 - cpu1) / self.sample_interval * 100
            mem_mb = mem / (1024 * 1024)
            if cpu_pct > self.cpu_threshold:
                lat = round((time.perf_counter() - start) * 1000, 1)
                return ProbeResult(False, "cpu_spike", f"pid={pid} cpu={cpu_pct:.0f}%", lat)
            if mem_mb > self.mem_threshold_mb:
                lat = round((time.perf_counter() - start) * 1000, 1)
                return ProbeResult(False, "mem_high", f"pid={pid} mem={mem_mb:.0f}MB", lat)
        lat = round((time.perf_counter() - start) * 1000, 1)
        return ProbeResult(True, "ok", f"监控 {len(s2)} 进程", lat)


# 探测类型到类的映射（供统一入口按字符串分派）
PROBE_TYPES = {"http": HTTPProbe, "process": ProcessProbe, "db": DBProbe,
               "disk": DiskProbe, "resource": ResourceProbe}


def make_probe(probe_type: str, **kwargs):
    cls = PROBE_TYPES.get(probe_type)
    if cls is None:
        raise ValueError(f"未知探针类型: {probe_type}")
    return cls(**kwargs)
