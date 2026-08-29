# 多AI评审

> 提示词: # Local Service Self-Healing Agent — 阶段 5 评审请求

请以资深 SRE/后端工程师视角评审这个"自愈 Agent"作品的**阶段 5（出口层）**设计。项目是黄文浩的求职作品集项目，会在面试中向面试官演示。你的任务：挑毛病、给改进建议，重点是"面试演示效果"...

## deepseek 回复

计链路是否可回放。目前的实现偏“成功恢复”演示，缺少“不安全动作被拒绝”的一键展示；审计文件之间关联不足，面试官一旦追问“某次 incident 当时策略门为什么拒绝”，你很难快速回溯。

下面按优先级给 8 条建议。

1. 增加 run --dry-run 和“不安全动作被拒绝”的演示场景，把策略门变成核心卖点

为什么重要
你的核心哲学是“LLM 只能建议，确定性策略才允许执行，确定性验证器才允许宣布恢复”。但当前演示路径是：注入 proc_crash → run 自动 restart → 恢复。这证明了“能修”，没有证明“不会乱修”。面试官最容易挑战的恰恰是：“如果 LLM 给了危险建议怎么办？” 如果演示里没有这一刀，核心安全设计就只是口头承诺。

怎么改

CLI 增加 run --dry-run：输出诊断结果、候选动作、策略门判定（ALLOW/BLOCK）和原因，但不执行。

在 mock 故障矩阵中加一个场景，例如 unsafe_kill_third_party 或 unscoped_restart，让 LLM 建议重启一个不在 service.yaml 授权范围内的服务，策略门拒绝，只 escalate。

Dashboard 增加 “Policy Gate” 小卡片，显示允许/拒绝次数；状态流里把 BLOCKED/ESCALATED 用红色标出。

面试剧本可以设计为：先跑一次成功恢复，再跑一次 --dry-run 或危险注入，让面试官看到 [BLOCKED] 和审计原因。这比任何架构图都有冲击力。

2. 审计日志增加 incident_id 关联和 schema version，支持 incident show <id> 回放决策链

为什么重要
现在 incidents.jsonl 和 policy.jsonl 是两套独立文件，没有关联主键。面试官如果问“这条 incident 当时诊断结果是什么？策略门依据哪个规则拒绝？”，你只能手动翻两个文件对时间戳。这会让“审计可回放性”大打折扣。

怎么改

每次生命周期生成一个 incident_id，例如 inc-20260830T103000Z-xxxx。

两个文件都写入 incident_id、ts（ISO8601 UTC）、schema_version: 1。

增加 CLI 命令：

incident <id>：从两个 jsonl 中按 incident_id 合并输出完整决策链：probe → diagnosis → policy → action → verify。

可附带 --json 输出，便于进一步分析。

Dashboard 点击某条 incident 时，弹出这个决策链，而不是只显示一行状态。

这个改动成本不高，但会让“审计”从日志文件升级成可取证、可回放的证据链。

3. CLI 的 --json 输出必须是稳定、结构化、可脚本化的；错误也要用 JSON envelope，退出码非 0

为什么重要
你提到“try/except 友好报错，不裸 traceback”，这只保证了人看舒服。但面试中如果你说“可以接告警/CI”，面试官可能让你现场把 status 输出 pipe 给 jq 或脚本。如果错误时输出的是人类文本，管道会坏。

怎么改
定义统一结构：

成功：

json
复制
下载
{"ok": true, "data": {...}}

失败：

json
复制
下载
{"ok": false, "error": {"code": "PROBE_FAILED", "message": "...", "service": "rag_qa"}}

所有命令失败时返回非 0 退出码。

--json 模式下完全不要混入人类可读文本。

run 增加 --timeout 参数，避免真实服务挂起导致演示卡住。

这样你的 CLI 就从“演示工具”变成“可集成的运维工具”，工程严谨性明显提升。

4. Dashboard 状态流要做视觉降噪和失败分支强化，不要只渲染英文状态词

为什么重要
非技术面试官或高管看到 DEGRADED → DIAGNOSING → REPAIRING → VERIFYING → HEALTHY 可能没有感觉。尤其失败路径 ESCALATED 如果不醒目，很容易被误以为流程正常结束。你的目标是让人一眼看出“系统检测到问题，尝试修复，成功/失败”。

怎么改

状态节点用颜色和图标：绿 HEALTHY、黄 DEGRADED、蓝 DIAGNOSING、紫 REPAIRING、红 ESCALATED、灰 UNKNOWN。

失败分支用红色加粗，并展示根因摘要，例如 ESCALATED: policy denied restart (service not in allowlist)。

默认只展示最近 10 条，提供按服务/状态/时间筛选，避免 50 条全糊在屏幕上。

指标卡不要从 eval_report.md 解析，改为读取机器可读的 eval_summary.json。Markdown 解析非常脆弱，面试现场如果格式稍变就会显示错误。

5. 修正 status 故意不做进程判重的工程债，提供显式模式而不是静默忽略

为什么重要
你提到“故意不做 expected_count 判重，避免 Windows venv shim 多进程误报”。这个决策可以理解，但面试官看到 status 输出时可能质疑：“如果有两个重复进程，它还说 OK？” 这会削弱探针可信度。

怎么改

在 service.yaml 中把 expected_count 设为 null 或 auto，明确表示“跳过重复检查”，而不是 CLI 层硬编码忽略。

status 默认输出聚合后的有效副本数，并加 --show-duplicates 查看原始进程列表。

在 CLI 帮助或注释里写明：“重复进程判重逻辑仅在评估期探针启用，Windows 下因 venv shim 可能误报，展示层跳过。”

这样既保留了演示可用性，又不会让懂行的面试官觉得你在掩盖问题。

6. rag_qa 只读监控要演示“真实故障 + 越权被拒绝”，而不是静态 404

为什么重要
现在演示 rag_qa 的 /healthz 404 导致 [FAIL]，只能说明探针能发现 HTTP 404，不能体现“真实服务监控”和“只读边界”的价值。面试官可能觉得这只是配置错误，或者本来就没有 health endpoint。

怎么改

为 rag_qa 定义一个合理健康探针，比如 TCP 端口 8000 可达 + HTTP / 返回 200。

演示时先 status rag_qa 显示 OK，然后手动停掉知识库或改端口。

再 status rag_qa 显示 FAIL。

关键步骤：执行 run rag_qa，输出被策略门拒绝自动重启，只执行 escalate，并记录一条 policy audit 拒绝日志。

Dashboard 给 rag_qa 加“read-only / escalation only”徽章。

这会让“只读监控”从鸡肋变成亮点：能发现真实服务故障，但不会越权操作。

7. 补充 config show、logs、metrics 等基础运维命令，完善 CLI 能力闭环

为什么重要
目前 CLI 只有 services/status/run/verify/incidents/audit/dashboard-data。面试时如果被问“当前 service.yaml 生效配置是什么？”“Agent 自己运行状态怎么看？”，你会没命令可用。这些是运维基础能力，缺失会显得工具链不完整。

怎么改

config show [service]：读取并输出生效配置，注意脱敏。

logs [--level]：查看 Agent 自身运行日志，支持 tail。

metrics：输出累计探针次数、修复次数、拒绝次数、平均 MTTR 等。

这些数据可以和 dashboard 指标卡复用，避免重复解析。

成本不高，但能显著提升“这是一个严肃运维工具”的印象。

8. 阶段 6 README/面试准备写成“三个演示剧本 + 架构决策记录”，而不是功能列表

为什么重要
面试官时间有限，README 如果按模块罗列，会被跳过。你需要让他 30 秒内抓住核心，并知道怎么演示。

怎么改
写三个演示剧本：

剧本 A：确定性自愈闭环（30 秒）
注入 proc_crash → status FAIL → run → 恢复 → verify OK。

剧本 B：策略门拒绝危险动作（1 分钟，核心）
run --dry-run 或注入 unsafe 场景 → 展示 [BLOCKED] + 审计拒绝原因。

剧本 C：真实服务只读监控（30 秒）
停掉 rag_qa → status FAIL → run 被拒 → escalate。

每个剧本写清准备命令、预期输出、可能追问点。
再加一个 TECH_DECISIONS.md，记录为什么 LLM 只建议不执行、为什么规则+LLM 双诊断、为什么默认关闭 LLM 增量，直接回应面试挑战。
最后，把 dashboard-data 生成命令放进 Makefile 或一键脚本，避免演示现场手工生成。

总体来看，阶段 5 已经具备演示基础，但最需要补的是一刀见血的“安全拒绝”场景，以及让审计链路可回放的关联 ID。这两件事做完，面试冲击力会完全不同。

## chatgpt 回复

/ 验证 / 调查
真正缺的是一个非常符合 SRE 习惯的：
Bashselfheal logs <service>
因为现场遇到问题时，工程师第一反应就是：

“服务到底报了什么？”

而 metrics 在你这个项目当前阶段反而不是必须的。没有真实 Prometheus / OpenTelemetry / time-series backend 的情况下，硬加一个 metrics 很容易变成“为了像生产系统而加”。
怎么改
优先加：
Bashselfheal logs demo-service
selfheal logs demo-service --tail 50
selfheal logs demo-service --incident INC-xxx
尤其最后一种非常漂亮：

incident → action → verifier → logs

形成排障闭环。
config 可以作为次优先：
Bashselfheal config show demo-service
但不要做成复杂配置编辑器。

P1-5：审计日志 Schema 必须从“能打印”提升到“可重建 Incident”
为什么重要
当前 incidents.jsonl / policy.jsonl 最大风险，不是有没有轮转，而是：

两个 JSONL 是否足以重新构造一次完整事件？

如果以后出现：
incident.jsonl
policy.jsonl
executor log
verifier log
靠 timestamp 去拼接，会逐渐变得脆弱。
怎么改
所有事件都建立统一的：
incident_id
trace_id
timestamp
service
stage
event_type
其中至少：
JSON{
  "incident_id": "INC-20260830-001",
  "trace_id": "TRC-001",
  "timestamp": "...",
  "service": "demo-service",
  "stage": "policy",
  "event": "decision",
  "action": "restart",
  "decision": "allow",
  "reason": "safe_restart_for_proc_crash"
}
最关键的是：
每条日志都带 incident_id。
这样 incident show 才能做到真正的 replay：
incident_id
  ├── detection
  ├── diagnosis
  ├── policy
  ├── execution
  └── verification
而不是依赖“时间戳大概相近”。

P1-6：补最基础的日志轮转、原子写入和 corruption handling
为什么重要
你已经用了 JSONL 做 audit，这是正确方向，但工程严谨性上，面试官很容易追问：

“日志一直增长怎么办？”
“程序写一半崩了怎么办？”
“最后一行 JSON 坏了怎么办？”

现在如果没有回答，会显得“像 demo，而不像后端系统”。
不需要上 ELK，也不需要复杂数据库。
怎么改
最小工程版就够：
audit/
  incidents.jsonl
  incidents.jsonl.1
  incidents.jsonl.2
  policy.jsonl
  policy.jsonl.1
增加：


size-based rotation，例如 5 MB


保留最近 3~5 个文件


append 写入


单条 JSON decode failure 不拖垮整个读取命令


malformed line 显示 warning 并继续


CLI：
[WARN] skipped malformed audit record at line 381
这样你在面试中可以明确说：

“我没有为了作品集引入数据库，但对本地审计日志做了最基本的 durability 和 corruption handling。”

这是很好的工程味。

P1-7：真实 rag_qa 监控不是鸡肋，但 Demo 角度需要改变
为什么重要
现在的：
rag_qa
只允许 escalate
其实是一个很好的安全边界案例，但目前叙事容易变成：

“因为真实服务比较危险，所以我不让它自动重启。”

这听起来像“没敢做”。
实际上应该把它讲成：

策略门明确区分 mock 可控服务与真实生产-like 服务。

也就是说：
demo-service
  proc_crash → restart allowed

rag_qa
  http_404 → observe + diagnose
            → auto-remediation forbidden
            → escalate
这恰恰证明你的系统不是：

“Agent 看见坏了就 restart。”

而是：

动作权限由服务级 policy 决定。

怎么改
最好再增加一个非常漂亮的演示：
status rag_qa
→ FAIL http_404

run rag_qa

Diagnosis:
  health endpoint incompatible / unhealthy

Policy:
  RESTART = DENY
  ESCALATE = ALLOW

Final:
  ESCALATED
  No destructive action executed
然后说一句：

“对未知真实服务，我宁可不修，也不能证明错修是安全的。”

这句话非常适合面试。

P2-8：阶段 6 不要继续疯狂堆功能，而应该做“工程证据层”
为什么重要
阶段 1~5 已经很完整了。下一步最大的风险是：

为了让项目显得更大，不停加功能，结果项目越来越像一个半成品运维平台。

对于求职作品，这反而危险。
阶段 6 应该解决的是：
“我怎么证明这个系统设计是合理的？”
而不是：
“我还能再加什么命令？”
怎么改
我建议阶段 6 直接收敛成三份材料。
README
按这个结构：
1. Problem
2. Design Principle
3. Architecture
4. Failure Matrix
5. Safety Model
6. Evaluation Results
7. Runtime Demo
8. Auditability
9. Limitations
10. Future Work
最重要的是把核心哲学放在最上面：

LLM proposes. Policy decides. Deterministic verifier proves recovery.

中文可以作为副标题。

tech_selection.md
不要写成“我用了 Python，因为方便”。
应该记录关键 trade-off：
Rule vs LLM
JSONL vs SQLite
CLI vs Web UI
Mock service vs Docker
Deterministic verifier vs LLM judge
Auto-repair vs Escalation
每项回答：
Requirement
Options
Decision
Trade-offs
Why not alternatives
这个文档会非常适合面试官深入追问。

interview-prep.md
不要准备“项目介绍稿”，而要准备攻击面。
例如：
Q: 为什么 verifier 不能交给 LLM？
Q: 为什么 policy 和 diagnosis 要分开？
Q: 如果 restart 本身失败怎么办？
Q: 如果 verifier 错误怎么办？
Q: LLM 给出错误 action 怎么办？
Q: 为什么 rag_qa 不允许 restart？
Q: JSONL 为什么不用数据库？
Q: 35 个场景够不够？
Q: 100% 是否意味着过拟合？
Q: MTTR 8.1s 怎么测？
Q: 如果服务不断 crash loop 怎么办？
每个问题准备：
结论 → 设计依据 → 当前限制 → 下一步
这会比再开发一个功能更能提升求职价值。

最后给一个总体评价
从资深 SRE/后端视角，我会给阶段 5 8.5/10。
真正值得保留的不是 CLI 或 Dashboard 本身，而是它们把阶段 1~4 的核心理念变成了可以现场观察、检查和追责的系统。
目前最大的缺口也很明确：

缺少一个以 incident_id 为中心的完整事件回放视图。

因此我会把下一步优先级排成：
① Incident Explain/Replay
② Runtime 与 Evaluation Dashboard 分离
③ 统一 audit schema + incident_id
④ 日志轮转/损坏恢复
⑤ logs 命令
⑥ 最后再做 README / tech_selection / interview-prep
而不是继续加更多“看起来像生产系统”的功能。
你这个项目面试时最强的一句话，其实已经基本形成了：

“我没有让 LLM 直接控制系统，而是让 LLM 负责提出诊断，确定性策略决定能不能执行，确定性验证器决定到底有没有恢复。”

这句话比 Dashboard 有多少卡片、CLI 有多少命令，更可能让 AI Agent/SRE 面试官记住这个项目。
