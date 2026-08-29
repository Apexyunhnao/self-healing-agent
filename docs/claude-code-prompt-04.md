# Claude Code 指令 04 —— 阶段 3：编排层（状态机 / 策略门 / 双诊断器 / 编排器）

> 复制下面「指令正文」给 Claude Code（终端版）。本任务由羔丸用 CLI 后台执行。

---

## 指令正文

**背景**：我们在做「Local Service Self-Healing Agent」（E:\Projects\self-healing-agent）。阶段 1（mock 环境+30 场景）和阶段 2（确定性模块 probe/executor/verifier + service.yaml）已完成验收。现在是**阶段 3：编排层**——这是项目的灵魂，把"感知→诊断→守门→执行→验证→兜底"串成状态机驱动的完整闭环。

**灵魂原则**：LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器可以宣布恢复。

**架构位置**（已有代码 selfheal\probe.py / executor.py / verifier.py / config.py，直接 import 使用）：
```
探针(probe) → 状态机(state_machine) → 诊断(rule_diagnoser + llm_diagnoser)
→ 策略门(policy) → 执行(executor) → 验证(verifier) → 记录(incident/audit log)
```

**任务**（全部在 selfheal\ 下）：

1. `selfheal\models.py`：共享数据模型
   - `@dataclass DiagnosisResult: root_cause: str, confidence: float, suggested_action: str, reason: str, diagnoser: str`（diagnoser = "rule"/"llm"）
   - `@dataclass Incident: service: str, incident_id: str, state_path: list, action: str, ok: bool, started_at: str, ended_at: str`
   - `@dataclass TickResult: service: str, state: str, probe_ok: bool, diagnosis: DiagnosisResult|None, action: str|None, verified: bool|None`

2. `selfheal\state_machine.py`：手写状态机（不引入框架）
   - 状态：healthy / degraded / diagnosing / policy_check / repairing / starting / verifying / recovered / rolling_back / retry / escalated / awaiting_human / quarantined
   - 转移规则（写死为常量表，状态转移必须是纯函数 transition(state, event) -> state）：
     - healthy --probe_fail--> degraded（连续 2 次探针失败才进 degraded，第一次失败记 pending_failures）
     - degraded --probe_ok--> healthy；degraded --probe_fail(第3次连续)--> diagnosing
     - diagnosing --diagnosis_ready--> policy_check；diagnosing --no_diagnosis--> escalated
     - policy_check --allowed--> repairing；policy_check --denied--> escalated
     - repairing --started--> starting；repairing --fail--> retry
     - starting --ready--> verifying；starting --timeout(30s)--> retry
     - verifying --pass--> recovered；verifying --fail--> retry
     - recovered --cooldown_done--> healthy
     - retry --budget_left--> repairing；retry --budget_exhausted(3次)--> escalated
     - escalated --human_ack--> awaiting_human；awaiting_human --service_fixed--> healthy（人工确认后）
     - 任何状态 --quarantine--> quarantined（连续 2 次 escalated 同一服务）
   - 状态机类 `StateMachine`：`__init__(service)`、`event(name)`、`state` 属性、`pending_failures` 计数、`retry_count`、`cooldown_until`（恢复后 30 秒冷却）、`per_service repair lock`（该服务在 repairing/starting/verifying 时拒绝新事件）

3. `selfheal\policy.py`：策略门（确定性）
   - `class PolicyEngine`：`__init__(service_config)`；`check(action: str, confidence: float, diagnosis: DiagnosisResult) -> (allowed: bool, reason: str)`
   - 规则：
     - action 必须在 service.yaml 的 repair.actions 里（mock_svc 有 restart/kill_stale/cleanup_logs/escalate；rag_qa 只有 escalate）
     - escalate 永远允许
     - confidence < 0.7 且 action 是 restart/kill_stale → 拒绝（走 escalate）
     - rag_qa（真实服务）只允许 escalate
     - kill_stale 必须带明确进程 pattern（service.yaml process.pattern），否则拒绝
   - 审计：每次 check 结果追加到 audit log（JSON 行，`audit\policy.jsonl`）

4. `selfheal\rule_diagnoser.py`：规则诊断器（baseline，确定性，不用 LLM）
   - `class RuleDiagnoser`：`__init__(service_config)`；`diagnose(probe_result: ProbeResult) -> DiagnosisResult`
   - 规则（按探针结果判断根因）：
     - probe 类型 http 且连接失败/超时 + 进程探针失败 → root_cause="process_down", action="restart", confidence=0.9
     - http 5xx → root_cause="http_error", action="restart", confidence=0.6（状态型故障，restart 可能无效——置信度低于 0.7 让 policy 拒绝自动 restart）
     - 进程在但 http 失败 → root_cause="unhealthy_but_alive", action="restart", confidence=0.5（保守，policy 会拦）
     - DB integrity 失败 → root_cause="db_corrupt", action="escalate", confidence=0.8（v1 不自动重构 DB）
     - 磁盘满（DiskProbe fail）→ root_cause="disk_full", action="cleanup_logs", confidence=0.9
     - 其他 → root_cause="unknown", action="escalate", confidence=0.3

5. `selfheal\llm_diagnoser.py`：LLM 诊断器（唯一 LLM 入口）
   - `class LLMDiagnoser`：`__init__(service_config, api_key=None)`；`diagnose(probe_result, log_tail: str) -> DiagnosisResult | None`
   - 读 .env 的 DEEPSEEK_API_KEY（项目根 .env，.gitignore 忽略）；没有 key → 返回 None（上层走 Rule fallback + 日志提示）
   - 调 DeepSeek（OpenAI 兼容 https://api.deepseek.com，模型 deepseek-v4-flash 或 chat 类），httpx trust_env=False，超时 30s
   - prompt：给探针结果 + 服务类型 + 最近日志片段（log_tail 最多 2000 字符），要求输出 JSON：{root_cause, confidence, suggested_action, reason}
   - 输出校验：pydantic 解析（或 json.loads + 字段检查）；解析失败/动作不在白名单 → 返回 None（降级 Rule）
   - 返回前记录原始输出和解析结果到 audit\llm_raw.jsonl（评估用）

6. `selfheal\orchestrator.py`：主循环
   - `class Orchestrator`：`__init__(services_config, state_machines: dict, diagnoser_rule, diagnoser_llm=None, policy, executor, verifier)`
   - `tick(service_name) -> TickResult`：单次检查循环
     1. 探针 check（从 service.yaml 读 probe 类型和 URL，动态替换 ${MOCK_PORT}）
     2. 状态机事件：probe_ok / probe_fail（用 pending_failures 连续计数）
     3. 若进 diagnosing：先 Rule diagnoser，再 LLM diagnoser（有 key 时），选置信度高且 policy 允许的
     4. policy.check → 执行 → starting 等待 → verifier 验证 → recovered/retry/escalated
     5. 记录 Incident 到 audit\incidents.jsonl
   - `run_once(service_name)`：跑一个完整生命周期（demo 用）
   - `loop(interval=5)`：持续循环（Ctrl+C 停止），打印每次 tick 状态

7. 项目根 `demo_orchestrator.py`：在 mock 环境演示完整生命周期
   - MOCK_PORT=18080 起 mock_svc → 注入 proc_crash_01（进程被杀，restart 可修复的真实故障）→ 跑 Orchestrator.run_once → 打印状态路径（应 healthy→degraded→diagnosing→policy_check→repairing→starting→verifying→recovered→healthy）
   - 再演示 http_500_06（状态型故障）：restart 后 verify 失败 → retry → 3 次 → escalated（展示预算耗尽升级人工）
   - 收尾清理（recover + 杀进程 + 清状态）

8. `tests\` 新增单测（unittest）：
   - `test_state_machine.py`：转移表全覆盖（healthy→degraded 连续 2 次、degraded 恢复、retry 3 次 escalate、quarantine、cooldown）
   - `test_policy.py`：白名单拒绝、confidence<0.7 拒绝 restart、rag_qa 只 escalate
   - `test_rule_diagnoser.py`：各故障类型→根因/动作/置信度
   - `test_llm_diagnoser.py`：无 key 降级（None）、坏 JSON 降级（None）、合法 JSON 通过
   - `test_orchestrator.py`：mock 环境 run_once 完整路径

**运行验证**（必须全部跑通再报告）：
- `python -m unittest discover -s tests -v`（全部过）
- `python demo_orchestrator.py`（两条生命周期路径都打印出来）
- 环境变量：MOCK_PORT 默认 18080；.env 里 DEEPSEEK_API_KEY 不填也能跑（LLM 降级）

**完成后报告**：
1. 文件清单
2. 单测结果（几个 pass）
3. demo_orchestrator.py 两条生命周期路径的输出
4. LLM 诊断器降级验证结果（无 key 时行为）

**注意**：
- 确定性核心（state_machine/policy/rule_diagnoser/executor/verifier）禁止 import LLM
- httpx 一律 trust_env=False；subprocess 一律 encoding="utf-8", errors="replace", timeout
- 不要动 mockenv\ 和已有 selfheal\probe.py 等（只新增 + import 复用）
- .env 放项目根（.gitignore 里加 .env），内容只留 DEEPSEEK_API_KEY= 空占位，不要写任何真实 key
- 不提交 GitHub，不 git init；不用 rm -rf
- 如果缺包：python -m pip install httpx pydantic pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple
