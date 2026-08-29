# CLAUDE.md — Local Service Self-Healing Agent

> 本文件是 Claude Code 的常驻上下文。每次开工前必读。所有设计决策以 requirements.md 和 docs/fault-matrix-design.md 为准。

## 项目概述

本地服务自愈智能体（求职作品集收官项目）：运行在本机的自愈编排器，持续监控关键服务，故障时由 LLM/规则诊断根因，经 Policy Engine 校验后执行白名单修复动作，由确定性验证器证明修复成功；失败回滚，超预算升级人工。全程状态机驱动，故障生命周期可审计、可回放。

灵魂原则：**LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器可以宣布恢复。**

## 架构（5 层）

```
Probe(确定性探针) → Diagnoser(LLM+Rule 双诊断) → Policy Engine(策略门)
→ Executor(幂等执行器) → Verifier(确定性验证器) → Incident Store/Audit Log/Metrics
```

- 探针/执行器/验证器：确定性，禁止 LLM
- 诊断器：LLM（增强）+ Rule（baseline），同输出 schema（DiagnosisResult），Rule 是兜底
- Policy Engine：白名单校验，LLM 只能提议不能执行
- 状态机：手写，不引入框架

## 目录结构

```
E:\Projects\self-healing-agent\
├── CLAUDE.md              # 本文档
├── requirements.md        # 需求基线（指标数学自洽）
├── docs\
│   ├── project-plan-v2.md # 架构设计定稿
│   ├── fault-matrix-design.md  # 故障矩阵设计（30 场景清单）
│   └── review.md          # 双AI评审原文
├── mockenv\               # 阶段1：mock 测试环境（隔离评估用）
│   ├── mock_server.py     # HTTP mock 服务（子进程）
│   ├── mock_db.py         # SQLite mock DB
│   ├── fault_injector.py  # 故障注入器
│   └── scenarios\         # 故障矩阵 JSON
│       └── fault_matrix.json
├── selfheal\              # 阶段2-3：核心包
│   ├── probe.py / executor.py / verifier.py
│   ├── state_machine.py / policy.py
│   ├── rule_diagnoser.py / llm_diagnoser.py
│   └── orchestrator.py
├── config\service.yaml    # 服务声明式配置（阶段2）
├── eval\run_eval.py       # 阶段4：自动评估
├── cli\main.py            # 阶段5：CLI
└── tests\                 # 单测
```

## 技术栈

- Python 3.11+（标准库优先）+ httpx + pydantic
- 手写状态机（不用 LangGraph——状态有限、确定性优先）
- LLM 诊断：DeepSeek API，pydantic 校验输出，解析失败走 Rule fallback
- 回滚：配置文件 Git 管理，变更前自动 commit，回滚 = git revert

## 核心契约（阶段 2+ 实现时遵守）

### DiagnosisResult（LLM 与 Rule 同 schema）
```python
class DiagnosisResult(BaseModel):
    root_cause: str            # 根因描述
    confidence: float          # 0-1，<0.7 走保守动作
    suggested_action: str      # 必须在白名单内
    reason: str                # 理由
```

### 状态机状态
healthy → degraded → diagnosing → policy_check → repairing → starting → verifying → recovered → healthy
失败分支：rolling_back / retry / escalated → awaiting_human

### 白名单动作（幂等）
restart / kill_stale / cleanup_logs / git_revert_config / escalate

### 安全红线（违反 = 项目作废）
1. LLM 永远不能直接执行动作，必须过 Policy Engine
2. 故障注入只在 mockenv 隔离环境跑，真实服务只读探针
3. Unsafe Remediation Rate 必须为 0：执行器绝不杀非托管进程、绝不碰真实磁盘
4. 同一故障 3 次修复失败 → 强制升级人工 + quarantine
5. 所有外部命令必须设超时 + 退出码处理

## 评估约定

- 故障矩阵 30 场景（24 单一 + 5 mixed + 1 baseline），JSON 严格按 docs/fault-matrix-design.md
- 开发集/盲评集 8:2 切分（种子 42），盲评集开发期禁止运行
- 报表数字必须来自 run_eval.py 变量，禁止模板写死
- 指标口径见 requirements.md 第 4 节（检测率/修复成功率/MTTR/误报率/Unsafe/Escape/LLM Incremental Value/非法率）

## 当前进度

- 阶段 1 文档已定稿：requirements.md + docs/fault-matrix-design.md
- mockenv/ 待实现（见 docs/claude-code-prompt-01.md 首条指令）
- 阶段 2+ 未开始
