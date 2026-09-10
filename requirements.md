# Local Service Self-Healing Agent — Requirements（阶段 1）

> 求职作品集收官项目。本文档是需求基线，成功指标必须数学自洽，写进 README 前先核对。

## 1. 项目概述

**Local Service Self-Healing Agent（本地服务自愈智能体）**：运行在本机的自愈编排器，持续监控关键服务，故障时由 LLM/规则诊断根因，经策略门校验后执行白名单修复动作，由确定性验证器证明修复成功；失败回滚，超预算升级人工。全程状态机驱动，故障生命周期可审计、可回放。

灵魂原则：
> LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器可以宣布恢复。

## 2. 背景痛点

作者本机常年运行多个必须保持健康的服务（RAG 知识库 8000 / 聊天网关 / 调试浏览器 9224），现状：
- 服务挂了靠人发现、人手动重启（曾有多次崩溃经历）
- 已有 watchdog 脚本只告警不自动修复
- 企业视角：生产服务不可用 = 直接损失；SRE 核心诉求 = 自动发现→自动修复→证明修好→修不好回滚或叫人

## 3. 目标

1. **感知**：确定性探针持续监控服务（HTTP / 进程 / DB / 磁盘）
2. **诊断**：LLM 或规则诊断根因（同一输出 schema），Rule baseline 对照证明 LLM 价值
3. **守门**：Policy Engine 校验候选动作是否在白名单（含参数约束），越界一律拒绝
4. **执行**：白名单动作幂等执行（restart / kill stale / cleanup logs / git revert / escalate）
5. **验证**：确定性验证器证明修复真实生效（重试+指数退避，防启动未就绪误判）
6. **兜底**：回滚失败/超预算 → 升级人工 + quarantine，绝不无限重试
7. **可观测**：审计日志 + Incident Store + 指标，全程可回放

## 4. 成功指标（v1 验收标准）

口径：故障矩阵 35 场景 = 单一故障 24 + Mixed 5 + Baseline 6（含 5 个新增非故障基线，测误报）。
开发集/盲评集 8:2 切分（固定种子 42）：开发集 29 场景（23 故障 + 6 baseline）迭代用，盲评集 6 场景（5 单一 + 1 mixed）只做最终验收。

| 指标 | 定义 | 目标 | 换算说明 |
|---|---|---|---|
| 检测率 | 被探针发现的故障场景 / 故障场景总数（29，不含 baseline） | ≥ 96%（28/29） | 最多漏 1 个 |
| 修复成功率 | 最终回到 healthy / 故障场景总数（29） | ≥ 86%（25/29） | 允许 4 个走升级人工也算"正确处理"但不算修复成功 |
| MTTR | 注入故障到恢复 healthy 的时长均值 | < 60s（mock 环境） | 报告均值+中位数 |
| 误报率 | baseline 场景中误触发修复次数 / baseline 场景数 | 0/6 | 必须为 0 |
| Unsafe Remediation Rate | 执行了不该执行的动作次数 / 总修复尝试次数 | **0** | 必须为 0（安全底线） |
| Verification Escape Rate | 未真恢复却被判 recovered 次数 / recovered 判定总数 | **≈0** | 必须为 0（验证器可信） |
| LLM Incremental Value | LLM 在 Mixed 场景修复成功率 − Rule baseline 在 Mixed 场景修复成功率 | > 0（原假设 LLM 4/5=80% vs Rule 2/5=40%；**实测 +0pp**，见 README 第 6 节） | 验证"AI 是否真的带来增量" |
| LLM 输出非法率 | LLM 非 JSON/缺字段/白名单外动作次数 / LLM 诊断总次数 | ≤ 10% | 有 Rule fallback 兜底 |

**自洽检查（实测口径，与 README 第 6 节一致）**：检测率 28/29=96.6%、正确处理率 28/29=96.6%、修复成功率 13/22=59.1%（策略门主动升级 9 个，非修不好）、MTTR 均值 8.1s、误报 0/6、Unsafe=0、Escape=0。**LLM Incremental Value 实测 +0pp**（Mixed 4 个场景中 Rule 2/4 vs LLM 2/4）——需求阶段"LLM 应比规则强 +40pp"的假设未成立，该结论由 A/B 对照实验得出。以上数字写文档前必须从 run_eval.py 报表变量读取，禁止模板写死。

## 5. 范围（v1 做）

- 监控对象（service.yaml 声明）：
  1. RAG 知识库（HTTP 8000）
  2. Hermes Gateway（进程 + DB 完整性）
  3. 调试浏览器（CDP 9224）
  4. Agent 自身（orchestrator 存活，外部 watchdog）
- 故障族：Process / Port / HTTP / Startup / DB / Storage / Logs / Restart / Browser / Gateway / Mixed
- 白名单动作：restart / kill_stale / cleanup_logs / git_revert_config / escalate（全部幂等）
- 评估：隔离环境 mock 服务 + 故障注入器 + run_eval.py 自动评估
- 出口：CLI（selfheal status / run / verify）+ JSON 审计日志 + 健康面板（可选）

## 6. 非目标（v1 不做）

- 不自动重构/修复 DB 数据（只检测 + 有限 remediation + 升级人工）
- 不在真实服务上自动注入故障（只读探针；--dangerously-allow-real 需显式开启）
- 不跨机器/分布式
- 不自动升级软件版本
- 不允许 LLM 自由操作（无白名单外动作）
- Agent 自身崩溃由外部 watchdog 保障，不在自愈范围内
- 不追求 LangGraph 等框架（状态有限，手写状态机）

## 7. 技术约束

- Python 3.11+，标准库 + httpx + pydantic
- 手写状态机（healthy/degraded/diagnosing/policy_check/repairing/starting/verifying/recovered/rolling_back/retry/escalated/awaiting_human）
- service.yaml 声明式配置 + @probe / @action 注册机制（可扩展性证明）
- LLM 诊断：DeepSeek API，pydantic 校验输出（DiagnosisResult），解析失败走 Rule Diagnoser，置信度 < 0.7 走保守动作
- 回滚：配置文件 Git 管理，变更前自动 commit，回滚 = git revert；回滚失败直接升级人工

## 8. 交付物（六阶段）

1. 需求+数据：本文档 + mock 服务框架 + 故障注入器 + 故障矩阵 JSON（30 场景）
2. 骨架：probe.py / executor.py / verifier.py / service.yaml（独立单测）
3. 核心：state_machine.py / policy.py / rule_diagnoser.py / llm_diagnoser.py / orchestrator.py
4. 评估：run_eval.py + 指标报表 + 3 个失败案例分析
5. 出口：CLI + 审计日志 + 健康面板 + 真实服务只读监控
6. 复盘：README + tech_selection.md + interview-prep.md + dev-log
