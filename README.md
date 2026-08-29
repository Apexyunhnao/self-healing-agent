# Local Service Self-Healing Agent（本地服务自愈智能体）

> **LLM proposes. Policy decides. Deterministic verifier proves recovery.**
>
> LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器可以宣布恢复。

一个运行在本机的自愈编排器：持续监控关键服务，故障时由规则/LLM 诊断根因，经策略门校验后执行白名单修复动作，由确定性验证器证明修复成功；失败回滚，超预算升级人工。全程状态机驱动，故障生命周期可审计、可回放。

---

## 30 秒速览

**这是什么**：本地服务故障自愈 Agent——不是"挂了就重启"的脚本，而是**在安全执行边界内的受约束自主**（bounded autonomy）：Agent 负责诊断和动作选择，但所有执行都过确定性策略门，恢复必须由确定性验证器证明。

**关键证据**（35 个故障场景，开发集 + 盲评分离验证）：

| 指标 | 值 |
|---|---|
| 检测率 | 100%（开发集）/ 96.6%（全量含盲评） |
| 正确处理率 | 100%（开发集）/ 96.6%（全量） |
| Unsafe Remediation | **0**（全量 34 次修复执行） |
| Verification Escape | **0**（全量 17 次 recovered 判定） |
| 误报率 | **0**（6 个非故障基线） |
| MTTR | 均值 8.1s / 中位 9.2s |
| 自动修复率 | 59.1%（其余被策略门**主动**升级人工，非修不好） |
| 单测 | 127 |

**反直觉结论**：A/B 对照实验证明，当前探针粒度下 **LLM 相比确定性规则无增量**（修复 +0pp、正确处理 -5pp、MTTR +7.8s、成本 >0）——所以生产路径默认规则驱动，LLM 保留为可选诊断组件。**不是把 LLM 塞进系统里，而是用实验决定 LLM 应该待在哪里。**

---

## 1. Problem

本机常年运行多个必须保持健康的服务（RAG 知识库 8000 / 聊天网关 / 调试浏览器 9224）。现状：服务挂了靠人发现、人手动重启；已有 watchdog 只告警不自动修复。

生产视角：服务不可用 = 直接损失。SRE 核心诉求 = **自动发现 → 自动修复 → 证明修好 → 修不好回滚或叫人**。

## 2. Design Principle（为什么是 Agent 而不是 watchdog）

| 问题 | 回答 |
|---|---|
| 为什么需要 LLM？ | 未知日志语义/组合故障的根因判断需要语义理解 |
| 为什么 LLM 不能直接执行？ | 执行是高风险副作用，LLM 负责推理，**不负责授权** |
| 为什么需要 Executor？ | 白名单动作要幂等、可审计（先查状态再杀再启） |
| 为什么需要 Verifier？ | "是否恢复"是可重复、可审计的事实判断，**不依赖概率模型** |
| 为什么是 Agent 而不是 watchdog？ | watchdog 只会"挂了拉起"；本系统有完整生命周期：诊断→策略→执行→验证→兜底 |

## 3. Architecture

```
┌──────────┐    ┌──────────────┐    ┌───────────┐    ┌────────────┐    ┌──────────────┐
│  Probe    │───▶│  Diagnoser   │───▶│  Policy   │───▶│  Executor  │───▶│   Verifier   │
│ HTTP/进程 │    │ Rule + LLM   │    │   Engine  │    │  白名单动作 │    │ 确定性证明恢复 │
│ DB/磁盘   │    │ 同一 schema  │    │  置信度门  │    │  幂等执行   │    │  连续N次采样  │
└──────────┘    └──────────────┘    └───────────┘    └────────────┘    └──────────────┘
      │                ▲                │                 │                  │
      └────────────────┴────────────────┴─────────────────┴──────────────────┘
                              State Machine（13 状态）驱动
```

- **确定性核心**（probe / policy / executor / verifier / rule_diagnoser / state_machine）**禁止 import LLM**
- LLM 只在 diagnosing 阶段提供候选诊断，输出过 pydantic 校验（DiagnosisResult），解析失败自动降级 Rule
- 状态机手写（13 状态），不引入 LangGraph——状态有限、需要完全可控，手写更合适

## 4. Failure Matrix（35 场景，非拍脑袋）

**唯一口径图**（任何数字都能沿此追溯）：

```
35 场景（fault_matrix.json）
├── 6 baseline（非故障：健康/慢/告警日志/CPU 高/多进程 → 测误报）
└── 29 故障（24 单一 + 5 组合 mixed）
    └── 1 invalid（port_occupy_04：注入器 no-op，故障未生效 → 排除出指标）
        28 有效故障
        ├── 22 开发集故障（迭代用，固定种子 42）
        └── 6 盲评故障（holdout，冻结实现后一次性验收）
```

| 故障族 | 场景 | 预期处理 | 规则 | LLM |
|---|---|---|---|---|
| Process | 进程崩溃 / 强杀 / 僵尸 PID | restart | ✓ | ✓ |
| HTTP | 500 / 503 / 超时 | restart / escalate | ✓ | ✓ |
| Port | 端口被未知进程占用 | escalate（防误杀） | ✓ | ✓ |
| Startup | 配置缺失 / 配置损坏 | restart / escalate | ✓ | ✓ |
| DB | 锁 / 完整性 | escalate | ✓ | ✓ |
| Storage | 磁盘 >95% | cleanup_logs | ✓ | ✓ |
| Restart | 启动循环 / 启动超时 | restart+验证 | ✓ | ✓ |
| Browser/Gateway | CDP 挂 / 进程活着但端点死 | restart | ✓ | ✓ |
| Mixed | 组合故障（5 个） | 诊断优先 | ✓ | ✓ |
| Baseline | 健康/慢/告警/CPU 高/多进程（6 个） | 不误触发 | — | — |

故障注入全部在隔离 mock 环境（随机高位端口），**绝不触碰真实服务**。开发集/盲评集 8:2 切分（固定种子），开发期不运行盲评集。

## 5. Safety Model（安全底线，两个硬指标）

- **Unsafe Remediation Rate = 0**：执行器绝不杀非托管进程；动作必须过白名单 + 置信度门（<0.7 拒自动 restart）
- **Verification Escape Rate = 0**：只有确定性验证器连续 N 次采样通过才算 recovered（防间歇故障骗过）
- 真实服务（rag_qa）只允许 escalate，不允许自动 restart——**对未知真实服务，宁可不修，也不能证明错修是安全的**
- 同一故障 3 次修复失败 → 强制升级人工（防死循环）；连续 2 次升级 → quarantine

## 6. Evaluation Results

诊断器：Rule（确定性 baseline）。**分母说明**（防"文字游戏"误读）：

- **修复成功率** = 自动修复回到 healthy / 有效故障数。9 个故障被策略门**主动**升级人工（端口占用防误杀、配置损坏重启无用等），不是修不好——这正是安全模型的设计
- **正确处理率** =（安全修复 + 安全升级）/ 有效故障数。自愈系统的目标不是"所有故障都自动修"，而是"不确定时不要乱动"
- **Unsafe 分母** = 修复动作执行次数（含重试），不是故障数
- **Escape 分母** = recovered 判定总数
- **全量口径**：29 有效故障分母中 1 个 invalid 计入分母不计入分子（保守），故 28/29

| 指标 | 开发集（22 有效故障 + 6 基线） | 全量（含盲评 6，28 有效故障） | 说明 |
|---|---|---|---|
| 检测率 | 22/22 = **100%** | 28/29 = **96.6%** | 探针曾失败的故障 / 有效故障 |
| 修复成功率 | 13/22 = 59.1% | — | 最终回到 healthy（策略门主动升级 9 个） |
| 正确处理率 | 22/22 = **100%** | 28/29 = **96.6%** | 修复 + 安全升级都算正确处理 |
| Unsafe Remediation Rate | **0/26 = 0%** | 0/34 = 0% | 违规执行动作数 / 修复执行次数 |
| Verification Escape Rate | **0/13 = 0%** | 0/17 = 0% | escape / recovered 判定数 |
| 误报率 | **0/6 = 0%** | — | baseline 场景误触发 |
| MTTR | 均值 8.1s / 中位 9.2s / P90 10.4s | — | 只统计自动修复成功场景 |

**LLM Incremental Value 对照实验**（同一测试集，Rule-only vs LLM+fallback）：

| 指标 | Rule | LLM(+fallback) | 差异 |
|---|---|---|---|
| 修复成功率 | 59.1% | 59.1% | **+0pp** |
| 正确处理率 | 100% | 95.7% | **-5pp** |
| MTTR | 8.1s | ~16s | **+7.8s** |
| 成本 | 0 | ~$0.003/60 调用 | **>0** |

**结论**：当前探针粒度下，规则已覆盖所有故障判断，LLM 没有额外信息可利用——无增量，甚至因置信度过高导致更多 escape 风险。**所以 LLM 默认关闭，作为可选诊断模块保留**。

> 这不是"AI 失败了"，这是评估驱动的架构决策：不是把 LLM 塞进系统里，而是用实验决定 LLM 应该待在哪里。

## 7. Runtime Demo（三个演示剧本）

### 剧本 A：确定性自愈闭环（30 秒）
```bash
python -m selfheal.cli status mock_svc          # [OK]
python mockenv/fault_injector.py inject proc_crash_01
python -m selfheal.cli status mock_svc          # [FAIL] process dead
python -m selfheal.cli run mock_svc             # restart → 验证通过 → recovered
python -m selfheal.cli verify mock_svc          # [OK] 连续 3 次 HTTP 200
```

### 剧本 B：策略门安全边界（1 分钟，核心）
```bash
python -m selfheal.cli run mock_svc --dry-run   # 只诊断 + 策略门判定，不执行
# 输出：诊断 process_down conf=0.9 → restart [ALLOW] 允许
python -m selfheal.cli audit --last 5           # 看策略门审计：置信度、允许/拒绝、原因
python -m selfheal.cli incident <id>            # 回放单次故障完整决策链
```

### 剧本 C：真实服务只读监控（30 秒）
```bash
python -m selfheal.cli status rag_qa            # 知识库 8000 → [FAIL] http_404
python -m selfheal.cli run rag_qa               # 策略门拒绝 restart → escalate → 无破坏性动作
```

## 8. Auditability（可审计、可回放）

- `audit/incidents.jsonl`：故障生命周期（状态路径 / 动作 / 耗时 / incident_id）
- `audit/policy.jsonl`：策略门每次判定（动作 / 置信度 / 允许拒绝 / 原因 / incident_id）
- `python -m selfheal.cli incident <id>`：按 incident_id 合并两个文件，**回放完整决策链**
- JSONL 5MB 轮转 + 损坏行跳过（不拖垮读取）；schema_version 字段便于演进

## 9. Limitations（诚实边界）

- 单机、无分布式；真实服务只读监控，不自动注入故障
- DB 损坏只检测 + 有限 remediation，不自动重构数据
- 误报率统计样本仍偏小（6 个 baseline）；Unsafe/Escape 的 0 是在当前测试集上的结论
- Agent 自身崩溃由外部 watchdog 保障，不在自愈范围内
- 验证器与探针共享部分检查方法（HTTP/进程），存在"共享盲点"的理论风险——用多次采样 + 独立 verifier 实现缓解

## 10. Future Work

- 给 LLM 更丰富的输入（日志尾部 / 进程快照 / 配置 diff），在规则未覆盖场景重新评估 LLM 增量
- 扩大动作白名单（重载配置 / 清理缓存），每个动作仍过策略门参数校验
- --runs N 重复运行增强统计可信度；补更多 baseline 场景
- 部署示例：systemd / Windows 计划任务 + 外部 watchdog

---

## Quick Start

```bash
# 依赖
pip install httpx pydantic pyyaml

# 跑测试（127 个单测，阶段 1-5 回归）
python -m unittest discover -s tests

# 看 CLI
python -m selfheal.cli services
python -m selfheal.cli status
python -m selfheal.cli incidents --last 5

# 评估（隔离 mock 环境，35 场景）
python mockenv/fault_injector.py list
python eval/run_eval.py --runs 1

# Dashboard（单文件，无 CDN，双击打开）
python -m selfheal.cli dashboard-data
```

技术选型与 trade-off：见 [docs/tech_selection.md](docs/tech_selection.md)  
面试准备：见 [docs/interview-prep.md](docs/interview-prep.md)  
开发日志：见 [docs/dev-log.md](docs/dev-log.md)
