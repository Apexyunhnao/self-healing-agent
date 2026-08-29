# 技术选型与 Trade-off 记录

> 每个选择都是权衡后的决定，不是"图方便"。面试官深挖时用这份文档回答。

## 1. Rule Diagnoser vs LLM Diagnoser

| 维度 | 决策 |
|---|---|
| Requirement | 故障根因判断，既要确定性强又要能理解未知语义 |
| Options | 纯规则 / 纯 LLM / 双诊断器（Rule baseline + LLM 增强） |
| Decision | **双诊断器，同一输出 schema（DiagnosisResult），Rule 为默认路径，LLM 可选** |
| Trade-offs | Rule 确定、零成本、可解释；LLM 语义强但非确定、有成本延迟 |
| Why not alternatives | 纯 LLM 无法保证 Unsafe=0（置信度高 → 策略门放行更多 → escape 更多）；纯规则覆盖不了未知日志语义 |

**关键实验**：同一测试集 Rule-only vs LLM(+fallback)，LLM 增量 = +0pp 修复率、-5pp 正确处理率、+7.8s MTTR、成本 >0 → **默认关闭 LLM**。这不是失败，是评估驱动的架构决策。

## 2. 手写状态机 vs LangGraph / 状态机框架

| 维度 | 决策 |
|---|---|
| Requirement | 13 个有限状态、完全可控、确定性优先 |
| Options | LangGraph / 手写状态机类 |
| Decision | **手写**（state_machine.py） |
| Trade-offs | LangGraph 适合复杂多步推理/多 Agent 编排；本项目状态有限且需要完全可控（面试能讲清每个转移） |
| Why not | 引入框架反而增加黑盒：状态转移是安全关键路径，手写每一行都能审计 |

## 3. JSONL 审计 vs SQLite / 数据库

| 维度 | 决策 |
|---|---|
| Requirement | 故障生命周期可回放、可审计；单机、轻量 |
| Options | SQLite / JSONL 文件 / 时序库 |
| Decision | **JSONL（incidents.jsonl + policy.jsonl + llm_calls.jsonl），5MB 轮转** |
| Trade-offs | JSONL 追加写零依赖、易 grep、可 git；查询不如 SQL 方便；用 incident_id 关联两文件实现"回放" |
| Why not | 单机工具引入 DB 是过度设计；JSONL 追加写天然防"写一半损坏"，坏行跳过不拖垮读取 |

## 4. CLI + 静态 HTML vs Web 服务 / 面板框架

| 维度 | 决策 |
|---|---|
| Requirement | 演示、排查、给别人看；国内可打开 |
| Options | FastAPI 面板 / CLI + 单文件 HTML / 前端框架 |
| Decision | **CLI（argparse）+ 单文件 dashboard.html（原生 JS+CSS，无 CDN）** |
| Trade-offs | 无构建、file:// 双击可用、零部署；功能朴素（指标卡 + Incident 列表 + 状态流） |
| Why not | 面试演示不需要重前端；FastAPI 面板增加部署面，且浏览器 file:// 拉本地文件受限 |

## 5. Mock 服务 + 故障注入 vs Docker / 真实服务

| 维度 | 决策 |
|---|---|
| Requirement | 安全、可重复、可注入的评估环境 |
| Options | Docker Compose / 本机 mock 服务 |
| Decision | **本机 Python mock（随机高位端口）+ fault_injector.py 故障注入** |
| Trade-offs | 零容器依赖、启动快；故障注入可控（kill/占端口/改坏 DB/写坏配置）；与真实服务环境有差距 |
| Why not | 用户机器 Docker 环境不稳定；mock 足够覆盖故障矩阵 35 场景；真实服务只读监控单独演示 |

## 6. 确定性 Verifier vs LLM Judge

| 维度 | 决策 |
|---|---|
| Requirement | "是否恢复"必须可重复、可审计、可证明 |
| Options | LLM 判断恢复 / 确定性验证器 |
| Decision | **确定性验证器**（连续 N 次采样 + 重试 + 指数退避） |
| Trade-offs | 确定性可审计但依赖探针覆盖；LLM judge 灵活但概率性，"恢复"这种事实判断不能依赖概率模型 |
| Why not | Escape Rate 必须 0，LLM 无法承诺 |

## 7. 自动修复 vs 升级人工（Escalation）

| 维度 | 决策 |
|---|---|
| Requirement | 不确定时不要乱动（安全优先） |
| Options | 全部自动修 / 自动修+策略门拒绝升级人工 |
| Decision | **自动修 + 策略门主动升级**（置信度 <0.7 拒重启、非白名单动作拒、3 次失败强制升级、2 次升级 quarantine） |
| Trade-offs | 修复成功率 59.1% 不高，但正确处理率 100%、Unsafe=0、Escape=0 |
| Why not | 放开策略门换修复率 → Unsafe/Escape 上升，自愈系统失去意义 |

## 8. 探针信息粒度

| 维度 | 决策 |
|---|---|
| Requirement | 诊断器要有足够信息区分故障 |
| Options | 只给健康状态 / 给健康 + 日志尾部 + 进程快照 + 配置 diff |
| Decision | v1 给探针健康状态 + 日志尾部（llm_diagnoser 的 log_tail） |
| Trade-offs | 信息少 → Rule 全覆盖 → LLM 无增量（实验已证明）；信息多 → LLM 有增量但实现复杂 |
| Why not | v1 先验证"边界"（确定性核心 + 安全模型），v2 扩展输入再评估 LLM 增量——按实验数据做决策 |
