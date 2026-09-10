# Local Service Self-Healing Agent — 项目计划 v2（评审后修订）

> 基于 DeepSeek + ChatGPT 双评审修订。评审原文：docs/review.md

## 项目定位（README 一句话）

**在严格安全边界内自主完成 Incident Lifecycle 的 LLM-assisted Self-Healing Orchestrator。**

灵魂原则（README 首页）：
> LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器可以宣布恢复。
> The LLM may propose a repair, but only deterministic policy allows it, and only a deterministic verifier can declare recovery.

## 1. 核心架构（v2，加 Policy Engine）

```
┌──────────────────────────────────────────────────────────┐
│               orchestrator（守护循环 + Incident Manager）   │
└──────────────────────────────────────────────────────────┘
   │           │           │           │           │
   ▼           ▼           ▼           ▼           ▼
 Probe     Incident    Evidence    LLM         Rule
 (探针)     Model      Collector    Diagnoser   Diagnoser(baseline)
   确定性     生命周期    证据收集     唯一LLM      规则
                        │           │           │
                        │           ▼           ▼
                        │    candidate action（同 schema）
                        │           │
                        ▼           ▼
                     Policy Engine（策略门：白名单校验）
                        │
                   allowed? ──no──▶ escalated
                        │
                        ▼ yes
                     Executor（执行器，确定性，幂等）
                        │
                        ▼
                     Verifier（验证器，确定性，重试+退避）
                        │
              ┌─────────┴──────────┐
              ▼                    ▼
          recovered            failed
              │                    │
           cooldown          rollback(有限)/retry
                                    │
                            budget exceeded?
                              ├─no → retry
                              └─yes → escalated(quarantine)
```

横切能力：Audit Log（审计日志）、Incident Store（故障库）、Metrics（指标）。

**LLM unavailable / 输出非法 → Rule Diagnoser fallback → 再失败 escalate。**

## 2. 分层职责（面试话术）

| 层 | 组件 | 确定性 | 职责 |
|---|---|---|---|
| 感知 | Probe | ✅ | HTTP/进程/DB/磁盘 检查 |
| 决策 | Rule Diagnoser | ✅ | baseline：规则判断根因 |
| 决策 | LLM Diagnoser | ❌ | 增强：组合故障/未知日志语义理解 |
| 守门 | Policy Engine | ✅ | 校验候选动作是否在白名单（含参数约束） |
| 执行 | Executor | ✅ | 白名单动作，幂等 |
| 证明 | Verifier | ✅ | 修复是否真生效，重试+指数退避 |
| 记录 | Incident Store / Audit Log / Metrics | ✅ | 故障生命周期、全程审计、指标 |

## 3. 状态机（v2 完整版）

```
healthy ──探针失败──▶ degraded（连续2次探针失败）
degraded ──连续3次失败/严重──▶ diagnosing
diagnosing ──(LLM/Rule 输出候选动作)──▶ policy_check
policy_check ──允许──▶ repairing
policy_check ──拒绝──▶ escalated
repairing ──启动中──▶ starting（带启动超时，就绪后再验证）
starting ──就绪──▶ verifying
verifying ──通过──▶ recovered ──cooldown──▶ healthy
verifying ──失败且可回滚──▶ rolling_back ──验证──▶ (通过→recovered / 失败→escalated)
verifying ──失败不可回滚──▶ retry（计数）
retry ──未超 budget──▶ repairing
retry ──超 budget(3次)──▶ escalated → awaiting_human（quarantine 该服务，停止自动重试）
```

规则：
- 探针阈值：连续 2 次失败进 degraded，第 3 次进 diagnosing
- 所有外部命令设超时 + 退出码处理
- per-service repair lock：一个服务在修复中，其他故障排队不并发修
- cooldown：恢复后冷却 N 秒才允许再次进入修复（防抖动）
- 服务启动：starting 状态等待端口就绪（重试+退避），避免立即验证误判

## 4. 监控对象与故障矩阵（v2）

监控对象（service.yaml 声明，插件注册机制 @probe/@action 可扩展）：
1. RAG 知识库（8000）
2. Hermes Gateway
3. 调试浏览器（9224）
4. Agent 自身（orchestrator 存活，外部 watchdog 保障）

故障矩阵（24-30 场景，8:2 开发/盲评切分，固定种子）：
- Process：crashed / killed / stale process
- Port：被托管 PID 占用 / 被未知 PID 占用
- HTTP：timeout / 500 / 503
- Startup：config missing / config malformed / dependency unavailable
- DB：locked / integrity failure
- Storage：disk > 90% / disk > 95%
- Logs：excessive log growth
- Restart：startup loop / restart timeout
- Browser：CDP unavailable / process alive but endpoint dead
- Gateway：process alive but unhealthy
- **Mixed（5-8 个，LLM 的战场）**：HTTP 503 + DB lock / process alive + port alive + health fail / 配置部分被改 + 端口被异常进程占用
- Baseline：everything healthy（测误报）

## 5. 评估体系（v2 指标）

**开发集 vs 盲评集**（系统工程语言，不叫 train/holdout——不是训练模型）：
- 开发集迭代，盲评集只做最终验收，固定种子 42

**指标**：
| 指标 | 定义 | 目标 |
|---|---|---|
| 检测率 | 注入故障被探针发现比例 | 高 |
| 修复成功率 | 最终回到 healthy / 全部注入数 | 高 |
| MTTR | 注入→恢复时长 | 短 |
| 误报率 | 无故障时误触发修复 | 低 |
| 回滚正确率 | 该回滚场景正确回滚 | 高 |
| **Unsafe Remediation Rate** | 执行了不该执行的动作 / 总修复次数 | **必须=0** |
| **Verification Escape Rate** | 没真恢复却被判 recovered | **必须≈0** |
| **LLM Incremental Value** | LLM 在 mixed/未知故障的表现 − Rule baseline 表现 | >0 才保留 LLM |
| LLM 输出非法率 | 非 JSON/缺字段/白名单外动作 | 低（有 fallback） |

**DB 损坏边界**：v1 只做"安全检测 + 有限 remediation（重启/隔离损坏索引）+ 不自动重构数据 + 升级人工"。

## 6. 回滚机制（v2）

- 配置文件用 Git 管理，变更前自动 commit，回滚 = git revert
- 备份包含：配置 + 环境变量 + 启动参数
- 回滚后验证失败 → 直接升级人工，不再自动二次回滚

## 7. LLM 输出校验（v2）

- pydantic 定义 DiagnosisResult（根因/置信度/建议动作/理由）
- 解析失败 → 走 Rule Diagnoser
- 置信度 < 0.7 → 强制保守动作（仅重启或升级人工）
- 原始输出 + 解析错误全记录（评估用）

## 8. 故障注入安全性（v2，底线）

- **默认全部在隔离环境跑**：Python subprocess 起 mock 服务（简单 HTTP server），随机高位端口，注入故障（kill mock 进程/占 mock 端口/改坏 mock DB）
- 真实服务只提供**只读探针模式**，不注入故障
- 磁盘满用 tmpfs/小容量模拟，绝不碰真实磁盘
- CLI 提供 --dry-run / --dangerously-allow-real 双开关（后者醒目警告）
- 真实服务修复动作默认关闭，仅演示时手动开启

## 9. 自身可靠性

- 外部 watchdog 监控 orchestrator 存活，挂了自动拉起
- README 写明：Agent 自身故障不在自愈范围内，由外部 watchdog 保障
- systemd/Windows 计划任务 部署示例

## 10. 技术栈（确认）

- Python 3.11+ 标准库 + httpx + pydantic
- 状态机**手写**（确定性优先，状态有限不需要框架；面试答"LangGraph 适合复杂多步推理，这里状态有限且需要完全可控，手写更合适"）
- LLM 诊断：DeepSeek API，结构化 JSON 输出
- 出口：CLI + 结构化日志 + 健康面板（FastAPI，可选）
- service.yaml 声明式配置 + @probe/@action 注册机制（扩展性证明）

## 11. 里程碑（六阶段，v2）

| 阶段 | 交付 | 验收 |
|---|---|---|
| 1 需求+数据 | requirements.md + mock 服务框架 + 故障注入器 + 故障矩阵 JSON（24-30 场景） | 场景清单含 5-8 个 mixed，指标定义自洽 |
| 2 骨架 | probe.py / executor.py / verifier.py / service.yaml | 三模块独立单测通过 |
| 3 核心 | state_machine.py / policy.py / rule_diagnoser.py / llm_diagnoser.py / orchestrator.py | mock 服务上注入一个故障走完整闭环 |
| 4 评估 | run_eval.py（自动注入→记录轨迹→报表）+ Rule baseline vs LLM 对比 | 9 项指标出报表，Unsafe=0，Escape≈0，3 个失败案例分析 |
| 5 出口 | CLI + 审计日志 + 健康面板 + 真实服务只读监控 | 浏览器可演示，日志可回放 |
| 6 复盘 | README + tech_selection.md + interview-prep.md + dev-log | 每份文档能讲，金句在 README 首页 |

## 12. 面试叙事

三项目递进：RAG=嘴 → 客服=手 → 编排器=脑 → **自愈=稳（SRE+Agent 安全边界）**。

面试高频追问（提前准备 30 秒答案）：
- 为什么不用 LangGraph？→ 状态有限、确定性优先，手写更可控
- LLM 输出了白名单外的动作怎么办？→ Policy Engine 拒绝并升级
- 如何保证动作幂等？→ 执行前查状态，重复执行无副作用
- 回滚失败怎么办？→ 直接升级人工，不二次回滚
- 故障注入如何保证覆盖真实故障？→ 从真实崩溃记录反推场景清单
- 为什么要 AI？→ LLM Incremental Value 指标说话（mixed cases 上优于 Rule baseline）

## 13. 命名（待用户拍板）

- 选项 A：保持功能名 "Local Service Self-Healing Agent"（直白，简历/面试零歧义）
- 选项 B：加代号 "Aegis Local" / "Heimdall"（更亮，但面试官要先对应"为什么叫这个"）
- 选项 C：用定位句式做副标题："LLM-assisted Self-Healing Orchestrator"（ChatGPT 建议的定位，作为一句话介绍而非主名）
- 决策倾向：**主名用 A（直白）+ README 副标题用 C（亮）**。简历不绕弯子，深度靠内容不靠名字。

## 14. 待办清单（下一步）

1. [ ] 用户拍板命名
2. [ ] requirements.md（需求/痛点/指标/范围/非目标）——阶段 1
3. [ ] mock 服务框架 + 故障注入器 + 故障矩阵 JSON ——阶段 1
4. [ ] 生成 Claude Code 首条指令（含 CLAUDE.md，自包含）
