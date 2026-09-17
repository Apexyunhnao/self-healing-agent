"""selfheal/cli.py — 阶段 5：CLI 出口。

用法:
    python -m selfheal.cli services
    python -m selfheal.cli status [service] [--json]
    python -m selfheal.cli run <service> [--json]
    python -m selfheal.cli verify <service> [--json]
    python -m selfheal.cli incidents [--last N] [--json]
    python -m selfheal.cli audit [--last N] [--json]
    python -m selfheal.cli dashboard-data        # 导出 dashboard_data.js（dashboard.html 数据源）

- 所有命令 try/except 友好报错，不裸 traceback。
- --json 输出结构化数据，供 dashboard/脚本消费。
- 环境变量 SELFHEAL_AUDIT_DIR 可覆盖审计目录（测试隔离用）；SELFHEAL_NO_LLM=1 强制 Rule-only。
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIT_DIR = PROJECT_ROOT / "audit"
EVAL_REPORT = PROJECT_ROOT / "eval" / "eval_report.md"
CONFIG_FILE = PROJECT_ROOT / "config" / "service.yaml"

# Windows 控制台中文乱码兜底（旧终端默认 codepage 非 UTF-8）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class CLIError(Exception):
    """CLI 层友好错误（打印中文提示，不裸 traceback）。"""


# ---------- 基础工具 ----------

def _audit_path(name: str) -> Path:
    base = Path(os.environ.get("SELFHEAL_AUDIT_DIR", str(DEFAULT_AUDIT_DIR)))
    return base / name


def _load_services() -> dict:
    from selfheal.config import load_services
    return load_services(CONFIG_FILE)


def _read_jsonl(path: Path) -> list:
    """读 JSONL；文件缺失→[]，坏行跳过。"""
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# ---------- 探针 / 验证 ----------

def _probe_service(svc: dict) -> list:
    """实时探针聚合：返回 [(check_name, ProbeResult), ...]。复用 orchestrator 的参数解析。"""
    from selfheal.orchestrator import Orchestrator
    from selfheal.probe import HTTPProbe, ProcessProbe, ResourceProbe, make_probe

    probe = svc.get("probe") or {}
    ptype = probe.get("type", "http")
    checks = []
    if ptype == "http":
        if "url" not in probe:
            raise CLIError(f"服务缺少 probe.url 配置")
        p = HTTPProbe(probe["url"], timeout=probe.get("timeout", 5))
    else:
        p = make_probe(ptype, **Orchestrator._probe_kwargs(svc))
    checks.append((f"probe({ptype})", p.check()))

    process = svc.get("process") or {}
    if process.get("pattern") or process.get("name") or process.get("pid_file"):
        # status 只看「进程存活」，不带 expected_count 判重：判重是评估期
        # orchestrator 探针的职责，status 误报 duplicate 反而干扰演示。
        # （Windows 上 venv shim 会在命令行里多带一个同 pattern 的父进程。）
        pp = ProcessProbe(name=process.get("name"), pid_file=process.get("pid_file"),
                          pattern=process.get("pattern"))
        checks.append(("process", pp.check()))

    resources = svc.get("resources") or {}
    if resources:
        rp = ResourceProbe(
            cpu_threshold_pct=resources.get("cpu_threshold_pct", 80),
            mem_threshold_mb=resources.get("mem_threshold_mb", 200),
            sample_interval=resources.get("sample_interval", 1.0),
        )
        checks.append(("resource", rp.check()))
    return checks


def _verifier_kwargs(svc: dict) -> tuple[str, dict]:
    """按 service.yaml verifier 段构造 verify(kind, **kwargs)。"""
    v = svc.get("verifier") or {}
    kind = v.get("type", "http")
    kwargs = dict(v.get("params") or {})
    if kind == "http":
        if "url" not in v:
            raise CLIError(f"服务缺少 verifier.url 配置")
        kwargs.setdefault("url", v["url"])
        kwargs.setdefault("timeout", v.get("timeout", 5.0))
        kwargs.setdefault("retries", v.get("retries", 3))
        kwargs.setdefault("backoff", v.get("backoff", 1.0))
        kwargs.setdefault("samples", v.get("samples", 3))
        kwargs.setdefault("sample_interval", v.get("sample_interval", 0.5))
    elif kind == "process":
        process = svc.get("process") or {}
        kwargs.setdefault("name", process.get("name"))
        kwargs.setdefault("pid_file", process.get("pid_file"))
    elif kind == "db":
        kwargs.setdefault("path", v.get("path"))
    return kind, kwargs


# ---------- 各子命令 ----------

def cmd_services(services: dict) -> list:
    return [{"name": name, "display": cfg.get("display", name),
             "actions": (cfg.get("repair") or {}).get("actions", [])}
            for name, cfg in services.items()]


def cmd_status(services: dict, name: str | None) -> list:
    names = [name] if name else list(services)
    for n in names:
        if n not in services:
            raise CLIError(f"未知服务: {n}（可用: {', '.join(services)}）")
    rows = []
    for n in names:
        checks = _probe_service(services[n])
        rows.append({
            "service": n,
            "display": services[n].get("display", n),
            "ok": all(r.ok for _, r in checks),
            "checks": [{"type": t, "ok": r.ok, "status": r.status,
                        "detail": r.detail, "latency_ms": r.latency_ms}
                       for t, r in checks],
        })
    return rows


def _patch_audit_targets() -> None:
    """SELFHEAL_AUDIT_DIR 存在时，把 orchestrator/executor 的审计落盘改到覆盖目录（测试隔离）。"""
    base = Path(os.environ.get("SELFHEAL_AUDIT_DIR", ""))
    if not base:
        return
    import selfheal.executor as exec_mod
    import selfheal.orchestrator as orch_mod
    orch_mod.INCIDENTS_LOG = base / "incidents.jsonl"
    exec_mod.AUDIT_LOG = base / "audit_log.jsonl"


def cmd_run(services: dict, name: str, dry_run: bool = False) -> dict:
    if name not in services:
        raise CLIError(f"未知服务: {name}（可用: {', '.join(services)}）")
    svc = services[name]
    _patch_audit_targets()

    from selfheal.llm_diagnoser import LLMDiagnoser, load_api_key
    from selfheal.orchestrator import Orchestrator
    from selfheal.policy import PolicyEngine
    from selfheal.rule_diagnoser import RuleDiagnoser
    from selfheal.state_machine import StateMachine

    if os.environ.get("SELFHEAL_NO_LLM") == "1":
        llm = None
    else:
        llm = LLMDiagnoser(svc) if load_api_key() else None

    policy = PolicyEngine(svc, audit_path=_audit_path("policy.jsonl"))
    orch = Orchestrator(
        services, {name: StateMachine(name, cooldown_seconds=0)},
        diagnoser_rule=RuleDiagnoser(svc), diagnoser_llm=llm, policy=policy,
    )

    if dry_run:
        # 只诊断 + 策略门判定，不执行动作。展示「候选动作 + 允许/拒绝 + 原因」。
        orch._probe_service(svc)
        orch._open_incident(name)
        diag = orch._diagnose(name)
        inc = orch._open.get(name)
        inc_id = inc.incident_id if inc else None
        candidates = []
        for d in (orch._last_diag,):
            if d is None:
                continue
            allowed, reason = policy.check(d.suggested_action, d.confidence, d,
                                           incident_id=inc_id)
            candidates.append({
                "action": d.suggested_action,
                "confidence": d.confidence,
                "root_cause": d.root_cause,
                "allowed": allowed,
                "reason": reason,
            })
        orch._open.pop(name, None)
        return {
            "service": name,
            "dry_run": True,
            "diagnosis": {"root_cause": (diag.root_cause if diag else None),
                          "confidence": (diag.confidence if diag else None),
                          "suggested_action": (diag.suggested_action if diag else None)},
            "policy_decisions": candidates,
            "note": "dry-run：仅诊断与策略门判定，未执行任何动作",
        }

    ticks = orch.run_once(name)
    sm = orch.state_machines[name]

    final_state = sm.state
    ok = final_state == "healthy"
    action = next((t.action for t in reversed(ticks) if t.action), None)
    verified = next((t.verified for t in reversed(ticks) if t.verified is not None), None)
    inc = _last_incident(name)
    return {
        "service": name,
        "display": svc.get("display", name),
        "final_state": final_state,
        "ok": ok,
        "action": action,
        "verified": bool(verified),
        "state_path": sm.state_path,
        "incident_id": (inc or {}).get("incident_id"),
        "duration_s": _incident_duration_s(inc) if inc else None,
        "ticks": [{"state": t.state, "probe_ok": t.probe_ok,
                   "action": t.action, "verified": t.verified} for t in ticks],
    }


def cmd_verify(services: dict, name: str) -> dict:
    if name not in services:
        raise CLIError(f"未知服务: {name}（可用: {', '.join(services)}）")
    svc = services[name]
    from selfheal.verifier import verify
    kind, kwargs = _verifier_kwargs(svc)
    r = verify(kind, **kwargs)
    return {"service": name, "ok": r.ok, "attempts": r.attempts,
            "samples": r.samples, "detail": r.detail}


def cmd_incidents(last: int | None) -> list:
    rows = _read_jsonl(_audit_path("incidents.jsonl"))
    rows.sort(key=lambda r: r.get("ended_at") or r.get("started_at") or "", reverse=True)
    return rows[:last] if last else rows


def cmd_incident_show(incident_id: str) -> dict:
    """按 incident_id 合并 incidents.jsonl + policy.jsonl，输出完整决策链（可回放）。"""
    incidents = _read_jsonl(_audit_path("incidents.jsonl"))
    inc = next((r for r in incidents
                if r.get("incident_id") == incident_id or
                str(r.get("incident_id", "")).startswith(incident_id)), None)
    if inc is None:
        raise CLIError(f"未找到 Incident: {incident_id}（用 incidents --last N 查看最近记录）")
    policy_rows = [r for r in _read_jsonl(_audit_path("policy.jsonl"))
                   if r.get("incident_id") == inc.get("incident_id")]
    policy_rows.sort(key=lambda r: r.get("ts", ""))
    return {
        "incident": inc,
        "policy_chain": policy_rows,
        "chain": {
            "detection": [c for c in (inc.get("state_path") or [])],
            "policy": [{"action": r.get("action"), "confidence": r.get("confidence"),
                        "allowed": r.get("allowed"), "reason": r.get("reason")}
                       for r in policy_rows],
            "final": {"action": inc.get("action"), "ok": inc.get("ok"),
                      "ended_at": inc.get("ended_at")},
        },
    }


def cmd_logs(service: str | None, tail: int | None = None) -> list:
    """读服务日志（service.yaml log_dir/<service>.log），支持 --tail N。"""
    if service is None:
        raise CLIError("logs 需要指定服务名（可用: python -m selfheal.cli services）")
    if tail is not None and tail <= 0:
        raise CLIError("--tail 必须是正整数")
    from selfheal.config import load_services
    services = load_services(CONFIG_FILE)
    if service not in services:
        raise CLIError(f"未知服务: {service}（可用: {', '.join(services)}）")
    svc = services[service]
    log_dir = svc.get("log_dir")
    candidates = []
    if log_dir:
        candidates.append(Path(log_dir) / f"{service}.log")
    candidates.append(PROJECT_ROOT / "mockenv" / "logs" / f"{service}.log")
    override = os.environ.get("SELFHEAL_LOG_DIR")
    if override:
        candidates.insert(0, Path(override) / f"{service}.log")
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if tail:
        lines = lines[-tail:]
    return [{"service": service, "line_no": i, "line": ln}
            for i, ln in enumerate(lines, 1)]


def cmd_audit(last: int | None) -> list:
    rows = _read_jsonl(_audit_path("policy.jsonl"))
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rows[:last] if last else rows


def cmd_dashboard_data(services: dict) -> dict:
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "services": cmd_services(services),
        "incidents": cmd_incidents(None),
        "policy": cmd_audit(None),
        "eval": _parse_eval_report(EVAL_REPORT),
    }
    out = PROJECT_ROOT / "dashboard_data.js"
    payload = ("window.DASHBOARD_DATA = "
               + json.dumps(data, ensure_ascii=False, indent=2) + ";\n")
    out.write_text(payload, encoding="utf-8")
    return {"written_to": str(out), "incidents": len(data["incidents"]),
            "policy": len(data["policy"])}


# ---------- Incident / 评估报告辅助 ----------

def _last_incident(service: str) -> dict | None:
    for r in reversed(_read_jsonl(_audit_path("incidents.jsonl"))):
        if r.get("service") == service:
            return r
    return None


def _incident_duration_s(inc: dict) -> float | None:
    try:
        s = datetime.fromisoformat(inc.get("started_at", ""))
        e = datetime.fromisoformat(inc.get("ended_at", ""))
        return round((e - s).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


_METRIC_NAMES = ("检测率", "修复成功率", "正确处理率", "Unsafe Remediation Rate",
                 "Verification Escape Rate", "误报率", "MTTR", "Mixed 修复")


def _parse_eval_report(path: Path) -> dict:
    """从 eval/eval_report.md 提取指标摘要（不存在返回空 dict）。"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    metrics = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in _METRIC_NAMES:
            metrics[cells[0]] = cells[1]
    generated = ""
    for line in text.splitlines():
        if line.strip().startswith("- 生成时间:"):
            generated = line.split(":", 1)[1].strip()
            break
    return {"metrics": metrics, "generated": generated}


# ---------- 文本输出 ----------

def _final_status(inc: dict) -> str:
    path = inc.get("state_path") or []
    if not path:
        return "FAIL"
    last = path[-1]
    return {"awaiting_human": "升级人工", "quarantined": "隔离"}.get(last, last)


def _print_text(command: str, data) -> None:
    if command == "services":
        for s in data:
            print(f"{s['name']:<12} {s['display']:<40} actions={s['actions']}")
    elif command == "status":
        total = len(data)
        ok_n = sum(1 for r in data if r["ok"])
        for r in data:
            print(f"== {r['service']} ({r['display']}) ==")
            for c in r["checks"]:
                tag = "[OK]" if c["ok"] else "[FAIL]"
                lat = f" ({c['latency_ms']}ms)" if c.get("latency_ms") is not None else ""
                print(f"  {c['type']:<16} {tag}  {c['status']}  {c['detail']}{lat}")
            print(f"  Overall: {'[OK]' if r['ok'] else '[FAIL]'}")
        print(f"共 {total} 服务: {ok_n} OK / {total - ok_n} FAIL")
    elif command == "run":
        d = data
        if d.get("dry_run"):
            print(f"[{d['service']}] DRY-RUN（未执行任何动作）")
            diag = d.get("diagnosis") or {}
            print(f"  诊断: 根因={diag.get('root_cause')}  置信度={diag.get('confidence')}  "
                  f"建议动作={diag.get('suggested_action')}")
            for c in d.get("policy_decisions", []):
                tag = "[ALLOW]" if c["allowed"] else "[DENY]"
                print(f"  策略门: {c['action']:<16} conf={c['confidence']}  {tag}  {c['reason']}")
            print(f"  说明: {d.get('note', '')}")
            return
        print(f"[{d['service']}] 生命周期开始")
        for i, t in enumerate(d["ticks"], 1):
            print(f"  tick {i}/{len(d['ticks'])}  state={t['state']:<12} "
                  f"probe_ok={t['probe_ok']}  action={t['action']}  verified={t['verified']}")
        tag = "[OK]" if d["ok"] else "[FAIL]"
        note = "已恢复" if d["ok"] else "升级人工/未恢复"
        print(f"  最终状态: {d['final_state']}  {tag}  {note}")
        print(f"  状态路径: {' → '.join(d['state_path'])}")
        if d.get("action"):
            print(f"  修复动作: {d['action']}    验证: {'通过' if d['verified'] else '未通过'}")
        if d.get("incident_id"):
            print(f"  Incident: {d['incident_id']}（耗时 {d['duration_s']}s）")
    elif command == "verify":
        d = data
        print(f"{d['service']}: {'[OK]' if d['ok'] else '[FAIL]'}  {d['detail']}")
    elif command == "incidents":
        if not data:
            print("（暂无 Incident 记录）")
        for inc in data:
            _print_incident(inc)
    elif command == "incident":
        d = data
        inc = d["incident"]
        print(f"=== Incident #{str(inc.get('incident_id', ''))[:8]} ===")
        print(f"服务: {inc.get('service')}   最终: {'已恢复' if inc.get('ok') else '未恢复'}  "
              f"动作: {inc.get('action') or '-'}")
        print(f"状态路径: {' → '.join(inc.get('state_path') or [])}")
        print(f"时间: {inc.get('started_at')} → {inc.get('ended_at')}")
        print("策略门决策链:")
        chain = d.get("policy_chain") or []
        if not chain:
            print("  （无策略门记录——可能由 CLI dry-run/手动触发）")
        for r in chain:
            tag = "[ALLOW]" if r.get("allowed") else "[DENY]"
            print(f"  {r.get('ts', ''):<21} {str(r.get('action', '')):<16} "
                  f"conf={r.get('confidence')}  {tag}  {r.get('reason', '')}")
    elif command == "logs":
        if not data:
            print("（暂无日志）")
        for row in data:
            print(f"{row['line_no']:>6} | {row['line']}")
    elif command == "audit":
        if not data:
            print("（暂无策略门审计记录）")
        for a in data:
            tag = "[ALLOW]" if a.get("allowed") else "[DENY]"
            conf = a.get("confidence", "")
            print(f"{str(a.get('ts', '')):<21} {str(a.get('service', '')):<24} "
                  f"{str(a.get('action', '')):<16} conf={conf}  {tag}  {a.get('reason', '')}")
    elif command == "dashboard-data":
        print(f"已写入 {data['written_to']}（incidents={data['incidents']}, policy={data['policy']}）")


def _print_incident(inc: dict) -> None:
    sid = (inc.get("incident_id") or "")[:8]
    status = "已恢复" if inc.get("ok") else _final_status(inc)
    action = inc.get("action") or "-"
    dur = _incident_duration_s(inc)
    dur_txt = f"{dur}s" if dur is not None else "-"
    print(f"#{sid}  {inc.get('service', ''):<10} {status:<6} {action:<16} 耗时 {dur_txt}")
    print(f"    {inc.get('started_at', '')} → {inc.get('ended_at', '')}")
    print(f"    {' → '.join(inc.get('state_path') or [])}")


# ---------- 入口 ----------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m selfheal.cli",
        description="Local Service Self-Healing Agent CLI",
    )
    parser.add_argument("--json", action="store_true", default=False,
                        help="输出 JSON（供 dashboard/脚本消费）")
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_json(p) -> None:
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    p = sub.add_parser("services", help="列出 service.yaml 配置的服务")
    _add_json(p)

    p = sub.add_parser("status", help="实时探针状态")
    p.add_argument("service", nargs="?", help="服务名，缺省列出全部")
    _add_json(p)

    p = sub.add_parser("run", help="跑一次完整生命周期（--dry-run 只诊断不执行）")
    p.add_argument("service")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="只诊断 + 策略门判定，不执行动作（演示安全边界）")
    _add_json(p)

    p = sub.add_parser("verify", help="只跑确定性验证器")
    p.add_argument("service")
    _add_json(p)

    p = sub.add_parser("incidents", help="查看最近 Incident")
    p.add_argument("--last", type=int, default=None, help="最近 N 条")
    _add_json(p)

    p = sub.add_parser("incident", help="查看单个 Incident 的完整决策链（回放）")
    p.add_argument("incident_id", help="Incident ID（支持前缀匹配）")
    _add_json(p)

    p = sub.add_parser("audit", help="查看策略门审计")
    p.add_argument("--last", type=int, default=None, help="最近 N 条")
    _add_json(p)

    p = sub.add_parser("logs", help="查看服务日志")
    p.add_argument("service", help="服务名")
    p.add_argument("--tail", type=int, default=None, help="最近 N 行")
    _add_json(p)

    p = sub.add_parser("dashboard-data", help="导出 dashboard_data.js")
    _add_json(p)
    return parser


def _error_out(args, message: str, code: int = 1) -> int:
    """统一错误出口：--json 时输出 {"ok": false, "error": {...}}，否则中文提示。"""
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": {"code": "CLI_ERROR",
                                                 "message": message}},
                         ensure_ascii=False), file=sys.stderr)
    else:
        print(f"[错误] {message}", file=sys.stderr)
    return code


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _build_parser().parse_args(argv)

    try:
        if args.command in ("services", "status", "run", "verify", "dashboard-data",
                            "incident", "logs"):
            services = _load_services()
        else:
            services = None

        if args.command == "services":
            data = cmd_services(services)
        elif args.command == "status":
            data = cmd_status(services, args.service)
        elif args.command == "run":
            data = cmd_run(services, args.service, dry_run=args.dry_run)
        elif args.command == "verify":
            data = cmd_verify(services, args.service)
        elif args.command == "incidents":
            data = cmd_incidents(args.last)
        elif args.command == "incident":
            data = cmd_incident_show(args.incident_id)
        elif args.command == "audit":
            data = cmd_audit(args.last)
        elif args.command == "logs":
            data = cmd_logs(args.service, tail=args.tail)
        elif args.command == "dashboard-data":
            data = cmd_dashboard_data(services)
        else:  # pragma: no cover
            _build_parser().print_help()
            return 0
    except CLIError as e:
        return _error_out(args, str(e), 2)
    except KeyboardInterrupt:
        return _error_out(args, "已取消", 130)
    except Exception as e:  # 兜底：不裸 traceback
        return _error_out(args, f"{type(e).__name__}: {e}", 1)

    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_text(args.command, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
