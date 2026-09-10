"""tests/test_eval.py — P0 整改后的评估层测试。

覆盖：
  - 故障矩阵 35 场景结构（单一 24 / mixed 5 / baseline 6）
  - port_occupy_04 invalid 标记 + 5 个新 baseline 场景
  - split_scenarios：开发集 29 / 盲评集 6，baseline 全进开发集，种子 42 确定性
  - compute_metrics：invalid 场景排除出分母，但计入分解叙述与 n_invalid
  - mock 新增 slow / warn / startup_blip 模式（不误触发）
  - 状态机对启动期单次探针失败容忍（baseline_starting_34 语义）
"""

import json
import sys
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
for _p in (str(BASE), str(BASE / "eval")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from run_eval import (ScenarioResult, compute_metrics, render_report,  # noqa: E402
                      split_scenarios)
from selfheal.state_machine import StateMachine  # noqa: E402

from common import (STATE_DIR, cleanup_state, free_port, http_get,
                    kill_mock_processes, start_mock, wait_healthz)  # noqa: E402

MATRIX = BASE / "mockenv" / "scenarios" / "fault_matrix.json"
LOG_DIR = BASE / "mockenv" / "logs"
SVC_STATE = STATE_DIR / "mock_svc.json"


def _load_matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _mk(sid: str, baseline: bool = False, cls: str = "repaired",
        detected: bool = True, status: str = "valid", final: str = "healthy",
        repair: int = 1, dur: float = 5.0, mixed: bool = False,
        diag_rule: int = 1, diag_llm: int = 0, run_index: int = 1) -> ScenarioResult:
    path = (["recovered"] if cls in ("repaired", "escape")
            else ["healthy", "degraded"])
    return ScenarioResult(
        scenario_id=sid, name=sid, family="x", mixed=mixed, baseline=baseline,
        detected=detected, final_state=final, classification=cls,
        repair_attempts=repair, state_path=path, duration=dur,
        test_status=status, diag_rule_count=diag_rule, diag_llm_count=diag_llm,
        run_index=run_index,
    )


class TestMatrixStructure(unittest.TestCase):
    def test_matrix_has_35_scenarios(self):
        data = _load_matrix()
        self.assertEqual(data["meta"]["total"], 35)
        scenarios = data["scenarios"]
        self.assertEqual(len(scenarios), 35)
        singles = [s for s in scenarios
                   if not s.get("mixed") and s["inject"]["method"] != "none"]
        mixed = [s for s in scenarios if s.get("mixed")]
        baseline = [s for s in scenarios if s["inject"]["method"] == "none"]
        self.assertEqual(len(singles), 24)
        self.assertEqual(len(mixed), 5)
        self.assertEqual(len(baseline), 6)

    def test_port_occupy_04_invalid_markers(self):
        data = _load_matrix()
        sid = "port_occupy_04"
        scen = next(s for s in data["scenarios"] if s["scenario_id"] == sid)
        self.assertEqual(scen.get("test_status"), "invalid")
        self.assertEqual(scen.get("invalid_reason"), "injector_noop")
        self.assertTrue(scen.get("excluded_from_metrics"))

    def test_five_new_baselines_present(self):
        data = _load_matrix()
        ids = [s["scenario_id"] for s in data["scenarios"]]
        for sid in ("baseline_slow_31", "baseline_warn_32", "baseline_cpu_33",
                    "baseline_starting_34", "baseline_multi_listen_35"):
            self.assertIn(sid, ids)
        for sid in ("baseline_slow_31", "baseline_warn_32", "baseline_cpu_33",
                    "baseline_starting_34", "baseline_multi_listen_35"):
            s = next(x for x in data["scenarios"] if x["scenario_id"] == sid)
            self.assertEqual(s["family"], "baseline")
            self.assertEqual(s["inject"]["method"], "none")


class TestSplitScenarios(unittest.TestCase):
    def test_dev_29_blind_6_deterministic(self):
        data = _load_matrix()
        scenarios = data["scenarios"]
        dev, blind = split_scenarios(scenarios)
        self.assertEqual(len(dev), 29)
        self.assertEqual(len(blind), 6)
        # 5 单一 + 1 mixed
        blind_single = [s for s in blind
                        if not s.get("mixed") and s["inject"]["method"] != "none"]
        blind_mixed = [s for s in blind if s.get("mixed")]
        self.assertEqual(len(blind_single), 5)
        self.assertEqual(len(blind_mixed), 1)
        # baseline 全进开发集（开发期测误报）
        dev_baseline = [s for s in dev if s["inject"]["method"] == "none"]
        self.assertEqual(len(dev_baseline), 6)
        # 种子 42 确定性
        dev2, blind2 = split_scenarios(scenarios)
        self.assertEqual(
            sorted(s["scenario_id"] for s in dev),
            sorted(s["scenario_id"] for s in dev2))
        self.assertEqual(
            sorted(s["scenario_id"] for s in blind),
            sorted(s["scenario_id"] for s in blind2))


class TestComputeMetricsInvalidExclusion(unittest.TestCase):
    def test_invalid_excluded_from_denominators(self):
        results = [
            _mk("f1", cls="repaired", dur=5.0),
            _mk("f2", cls="repaired", dur=10.0),
            _mk("f3", cls="handled", repair=0, final="awaiting_human"),
            _mk("port_occupy_04", cls="missed", detected=False, status="invalid",
                repair=0, dur=0),
            _mk("b1", baseline=True, cls="baseline_ok", repair=0, dur=0),
        ]
        m = compute_metrics(results)
        self.assertEqual(m["fault_total"], 4)          # 含 invalid
        self.assertEqual(m["valid_fault_total"], 3)    # 排除 invalid
        self.assertEqual(m["n_invalid"], 1)
        self.assertEqual(m["dec_repaired"], 2)
        self.assertEqual(m["dec_handled"], 1)
        self.assertEqual(m["dec_missed"], 1)           # invalid 计入分解叙述
        self.assertEqual(m["n_repaired"], 2)
        self.assertEqual(m["n_handled"], 1)
        self.assertEqual(m["n_missed"], 0)             # 有效故障无 missed
        self.assertEqual(m["n_detected"], 3)           # 有效故障全部检测到
        self.assertEqual(m["repair_attempts"], 2)      # invalid 的尝试不计
        self.assertEqual(m["n_baseline"], 1)
        self.assertEqual(m["n_baseline_fp"], 0)
        self.assertEqual(m["mttr_mean"], 7.5)
        self.assertEqual(m["mttr_median"], 7.5)
        # 有效故障 = 2 repaired + 1 handled
        self.assertEqual(m["n_recovered_judged"], 2)

    def test_baseline_false_positive_counts(self):
        results = [
            _mk("f1", cls="repaired"),
            _mk("b1", baseline=True, cls="baseline_fp", repair=1, dur=0),
            _mk("b2", baseline=True, cls="baseline_ok", repair=0, dur=0),
        ]
        m = compute_metrics(results)
        self.assertEqual(m["n_baseline"], 2)
        self.assertEqual(m["n_baseline_fp"], 1)


class TestRenderReportSections(unittest.TestCase):
    def test_runs_cumulative_note(self):
        # --runs 2：同一场景跑两遍（一修复一升级），报表应带累计口径行
        results = [
            _mk("f1", cls="repaired", dur=4.0, run_index=1),
            _mk("f1", cls="handled", repair=0, final="awaiting_human", dur=0, run_index=2),
            _mk("b1", baseline=True, cls="baseline_ok", repair=0, dur=0, run_index=1),
            _mk("b1", baseline=True, cls="baseline_ok", repair=0, dur=0, run_index=2),
        ]
        text = render_report(results, "rule", "测试", runs=2)
        self.assertIn("累计口径（--runs 2）", text)
        self.assertIn("runs=2", text)
        # 有效故障 2 次执行：1 修复 + 1 升级
        self.assertIn("2 次故障执行 = 1 修复 + 1 升级人工 + 0 未检测", text)

    def test_llm_cost_section_and_conclusion(self):
        results = [_mk("f1", cls="repaired", dur=4.0)]
        rule_m = compute_metrics(results)
        calls = [{"ts": "2026-08-29T00:00:00", "model": "deepseek-chat",
                  "input_tokens": 1000, "output_tokens": 200, "duration_s": 1.2}]
        text = render_report(results, "llm", "测试", rule_metrics=rule_m,
                             rule_results=results, runs=1, llm_calls=calls)
        self.assertIn("## LLM 成本报告（估算）", text)
        self.assertIn("LLM 调用总次数 | 1", text)
        self.assertIn("估算成本", text)
        # 结论区：LLM 与 Rule 一致 → repair +0pp，cost >0 → 默认关闭
        self.assertIn("repair +0pp", text)
        self.assertIn("推荐 Rule-only", text)

    def test_llm_no_key_shows_degradation_note(self):
        results = [_mk("f1", cls="repaired", dur=4.0)]
        text = render_report(results, "llm", "测试", llm_calls=[])
        self.assertIn("未配置 DEEPSEEK_API_KEY", text)


class TestStateMachineSingleFailureTolerance(unittest.TestCase):
    """baseline_starting_34 语义：启动期单次探针失败不应触发降级。"""

    def test_one_failure_stays_healthy(self):
        sm = StateMachine("mock_svc")
        sm.event("probe_fail")
        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.pending_failures, 1)

    def test_two_consecutive_failures_degraded(self):
        sm = StateMachine("mock_svc")
        sm.event("probe_fail")
        sm.event("probe_fail")
        self.assertEqual(sm.state, "degraded")

    def test_failure_then_ok_resets(self):
        sm = StateMachine("mock_svc")
        sm.event("probe_fail")
        sm.event("probe_ok")
        self.assertEqual(sm.state, "healthy")
        self.assertEqual(sm.pending_failures, 0)


class TestMockBaselineModes(unittest.TestCase):
    """mock 新增 slow / warn / startup_blip 模式的 HTTP 级验证（不触发修复）。"""

    @classmethod
    def setUpClass(cls):
        cleanup_state()
        kill_mock_processes()
        cls.port = free_port()
        start_mock(cls.port)
        if not wait_healthz(cls.port):
            raise RuntimeError("mock 服务未就绪")

    @classmethod
    def tearDownClass(cls):
        kill_mock_processes()
        cleanup_state()

    def setUp(self):
        SVC_STATE.unlink(missing_ok=True)  # 清 modes，回到健康
        if not wait_healthz(self.port, timeout=3):
            start_mock(self.port)
            if not wait_healthz(self.port):
                raise RuntimeError("mock 未恢复")

    def _set_state(self, state: dict) -> None:
        SVC_STATE.write_text(json.dumps(state), encoding="utf-8")
        time.sleep(0.4)  # Windows 写缓存：等 daemon 进程能读到新状态

    def test_slow_mode_healthz_still_ok(self):
        self._set_state({"modes": ["slow"], "slow_sec": 1.0})
        t0 = time.time()
        code, body = http_get(self.port)
        dur = time.time() - t0
        self.assertEqual(code, 200)
        self.assertIn('"status": "ok"', body)
        # 延迟 ~1s 生效，但远低于探针 5s 超时 → 不触发降级
        self.assertGreaterEqual(dur, 0.9)

    def test_warn_mode_logs_warning(self):
        self._set_state({"modes": ["warn"]})
        code, _ = http_get(self.port)
        self.assertEqual(code, 200)
        # log_line 在响应体发完后才写（服务端线程），立即读会看到上一条日志
        time.sleep(0.3)
        log_text = (LOG_DIR / "mock_svc.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("WARN", log_text)

    def test_startup_blip_recovers_after_first_failure(self):
        self._set_state({"modes": ["startup_blip"], "blip_count": 1})
        code1, _ = http_get(self.port)
        self.assertEqual(code1, 503)  # 启动期单次失败
        code2, _ = http_get(self.port)
        self.assertEqual(code2, 200)  # 立即恢复


if __name__ == "__main__":
    unittest.main()
