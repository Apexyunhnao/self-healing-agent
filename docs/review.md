# 多AI评审

> 提示词: # Local Service Self-Healing Agent — 项目计划（评审稿）

> 本文档自包含，请独立评审后给出：方案优缺点、遗漏的风险、改进建议。
> 背景：CS 应届生求职作品集项目（AI Agent 方向），此前基于翻墙代理的"自愈"方案因法律风险已废弃，本方案换为**本地服...

## deepseek 回复

时间在隔离测试环境和规则 baseline 上，而不是追求 LangGraph 或复杂 UI。

本回答由 AI 生成，内容仅供参考，请仔细甄别
评审意见：Local Service Self-Healing Agent
总体评价

这是一个方向正确、边界清晰、工程叙事完整的作品集项目。相比常见的“LLM 聊天机器人”或“RAG 问答”，这个项目展示的是可靠性工程 + AI Agent 安全边界，恰好命中企业招聘中对“能写、能排查、能证明”的候选人的期待。
核心亮点是把 LLM 放在“诊断”环节，用确定性探针和验证器包围它，这种设计比“让 LLM 自由操作”成熟得多，面试时能讲出深度。

但项目目前仍有一些为了 AI 而 AI、为了状态机而状态机的倾向，需要在落地时补齐工程细节，否则容易被面试官追问出漏洞。

一、方案优点

确定性层与 LLM 决策层分离
这是全文最值得保留的设计。探针、执行器、验证器全部确定性，LLM 只做根因诊断，且受限白名单动作。这直接回应了“Agent 安全边界”这个高频考点。

评估体系扎实
故障注入测试集 + 指标 + holdout 切分，比“我手动试了一下能跑”高一个维度。特别是误报率、回滚正确率、升级人工正确率这些指标，能体现对生产可靠性的理解。

状态机驱动
状态转移清晰，避免脚本式 if-else 堆叠。连续失败强制升级、幂等动作、结构化日志可回放，都是成熟 SRE 思维。

真实痛点
作者本机确实有多个需要保持健康的长驻服务，故障类型也常见（崩溃、端口占用、DB 损坏、磁盘满），不是虚构场景。这会让项目更有说服力。

面试叙事完整
从 RAG → 客服 → 编排器 → 自愈，递进逻辑成立，“稳”这个关键词与运维/Agent 岗位匹配。

二、缺点与遗漏风险
1. LLM 诊断的必要性未被证明

已知风险 1 很关键。在 v1 的 6 类故障中：

进程崩溃 → 规则可判断（进程不存在）

端口被占 → 规则可判断（lsof/ss）

HTTP 5xx → 规则可判断（状态码）

DB 损坏 → 规则可判断（PRAGMA integrity_check）

磁盘满 → 规则可判断（df）

配置错误 → 规则可判断（启动日志解析）

如果规则能覆盖 80% 场景，LLM 就会变成装饰品。
更糟的是，LLM 输出不稳定、可能返回非法 JSON、可能建议白名单外的动作，反而降低可靠性。

建议：

引入“规则诊断器”作为 baseline，LLM 作为增强诊断器。两者输出同一 schema，对比准确率、耗时、误诊率。

明确 LLM 的不可替代场景：例如“组合故障”（进程活着但端口被异常进程占用，同时配置文件被部分修改）、“未知错误日志”的语义理解。

在 README 中写清：为什么规则不够用，举一个真实案例。如果举不出来，就诚实降级 LLM 为可选模块。

2. 状态机细节不足，易被追问

当前状态机只有 healthy、degraded、diagnosing、repairing、verifying、recovered、escalated。但：

degraded 与 diagnosing 的边界模糊：连续失败几次进入 diagnosing？严重故障怎么定义？

验证失败后回滚：回滚动作本身可能失败，状态如何流转？

服务启动中：重启后服务需要时间监听端口，立即验证会误判。需要重试/指数退避。

人工介入后如何恢复：escalated 之后是人工确认还是自动重试？

建议：

增加明确阈值：例如“连续 2 次探针失败进入 degraded，第 3 次失败进入 diagnosing”。

增加 starting、rolling_back、awaiting_human 状态，并定义超时。

为所有外部命令设置超时和退出码处理。

3. 故障注入安全性会被重点质疑

已知风险 3 很现实。在本机 kill 真实服务、写满磁盘、改坏 DB，即使有备份也可能导致开发环境不可用，甚至影响其他项目。

建议：

默认所有故障注入在隔离环境运行：使用 Docker 容器或 Python subprocess 启动 mock 服务（如简单 HTTP server），端口使用随机高位端口。

真实服务仅提供“只读探针”模式，不注入故障。

如果必须用真实服务，加入 --dry-run 和 --dangerously-allow-real 双开关，并打印醒目警告。

磁盘满故障用 loop device 或小容量 tmpfs 模拟，绝不碰真实磁盘。

4. 监控对象太薄，扩展性未体现

3 个服务对于作品集足够了，但面试官可能会问：“如果再加 10 个服务，你的代码要改多少？”
建议：

设计 service.yaml 配置文件，声明每个服务的探针类型、启动命令、健康检查命令、回滚策略。

探针和动作做成插件注册机制（例如 @probe("http")、@action("restart")），展示可扩展性。

增加一个“Agent 自身健康检查”服务（监控 orchestrator 是否存活），体现自我监控意识。

5. 评估指标的统计意义弱

20 个故障场景，8:2 切分后 holdout 只有 4 个，无法支撑“修复成功率”等指标。
建议：

至少准备 40–50 个场景，或者使用 k-fold 交叉验证（如 5 折）。

明确指标分母：修复成功率 = 成功回到 healthy 的注入数 / 全部注入故障数，还是 / 检测到的故障数？两者差异很大。

增加“平均修复尝试次数”“回滚成功率”“LLM 输出非法率”等细分指标。

6. 回滚机制描述过于理想化

配置回滚依赖“变更前自动备份”，但：

备份何时触发？只备份配置文件还是整个服务目录？

备份保留几个版本？

如果回滚后服务仍然起不来怎么办？

建议：

使用 Git 管理配置文件，每次变更前自动 commit，回滚即 git revert。

备份必须包含服务依赖的环境变量、启动参数。

回滚后验证失败，直接升级人工，不再自动二次回滚。

7. LLM 输出 schema 校验缺失

LLM 可能返回非 JSON、缺字段、建议白名单外动作。
建议：

使用 pydantic 定义 DiagnosisResult，LLM 输出先解析，解析失败走规则诊断器。

对置信度低于阈值（如 0.7）的结果，强制走保守动作（升级人工或仅重启）。

记录 LLM 原始输出和解析错误，便于评估。

8. 自身可靠性与部署方式未提及

Agent 自己崩溃了怎么办？如何保证它常驻？
建议：

提供 systemd/launchd 配置文件示例。

增加“看门狗”脚本，监控 orchestrator 进程，发现挂掉自动拉起。

在 README 写明：Agent 自身故障不在自愈范围内，但可通过外部 watchdog 保障。

三、已知风险回应
问题	回应
LLM 诊断必要性	规则可覆盖大部分 v1 故障，LLM 应作为增强而非必需；建议对比评估，保留但降权。
手写状态机 vs LangGraph	手写更合适，能展示基础能力；LangGraph 会引入额外抽象，且本项目状态有限、确定性优先。面试时可直接说“状态机简单到不需要框架，LangGraph 适合复杂多步推理”。
故障注入安全	必须隔离，默认用 mock 服务/容器，真实服务只读探针。
监控对象太少	3 个够用，但要插件化设计展示扩展性，并增加自身健康检查。
命名与包装	名称可以更具体，见下文。
四、改进建议（按优先级）

隔离故障注入环境
这是底线。没有隔离，项目无法安全演示。

补齐状态机细节
定义阈值、超时、重试、人工恢复流程。最好画一张完整状态转移图（包含失败分支）。

增加规则诊断器作为 baseline
与 LLM 诊断对比，用数据证明 LLM 的价值。如果没有数据，就诚实标注 LLM 为可选模块。

插件化监控与动作
用 service.yaml 统一配置，探针/动作注册机制，展示扩展性。

强化验证器
支持重试、指数退避、业务探针（如 RAG 查询一个测试 query），避免“端口通了但服务不可用”。

准备面试追问清单
例如：

为什么不用 LangGraph？

LLM 输出了白名单外的动作怎么办？

如何保证动作幂等？

回滚失败怎么办？

故障注入测试集如何保证覆盖真实故障？
每个问题都要能 30 秒内答出要点。

项目命名建议
当前名“Local Service Self-Healing Agent”准确但稍显平淡。可以考虑：

Aegis Local（Aegis 是守护神，暗示保护）

Heimdall（北欧守门神，适合监控/守护）

Local SRE Agent（直接点出 SRE 能力）

副标题统一为：“A deterministic-verified self-healing agent for local services”
中文叙事用“本地服务自愈智能体”即可。

五、总结

这个项目骨架很好，但肉还不够。
核心风险不是技术路线错误，而是：

LLM 可能沦为噱头

状态机细节经不起追问

故障注入安全性可能毁了开发环境

评估指标样本量太小

如果能在 v1 中解决以上问题，这会是一个远超应届生平均水平的 Agent 作品集项目，面试时能形成“可靠性 + AI + 工程能力”的立体叙事。建议优先花时间在隔离测试环境和规则 baseline 上，而不是追求 LangGraph 或复杂 UI。

## chatgpt 回复

──────────┘
                                 │
                 ┌───────────────┼───────────────┐
                 ↓               ↓               ↓
             Probe         Incident        Policy/Guard
                              Manager           │
                 │               │              │
                 └───────┬───────┘              │
                         ↓                      │
                 Evidence Collector             │
                         ↓                      │
                  ┌──────────────┐              │
                  │ LLM Diagnoser│──────────────┘
                  └──────┬───────┘
                         │ candidate action
                         ↓
                  Policy Validation
                         │
                      allowed?
                    /           \
                  no             yes
                  ↓               ↓
              escalate         Executor
                                  │
                                  ↓
                              Verifier
                                  │
                       ┌──────────┴──────────┐
                       ↓                     ↓
                   recovered              failed
                       ↓                     ↓
                    cooldown          rollback/retry
                                             │
                                      budget exceeded?
                                         /       \
                                       no         yes
                                       ↓           ↓
                                     retry     escalated
再加三个横切能力：
Audit Log
Incident Store
Metrics
以及：
LLM unavailable
      ↓
Rule fallback
      ↓
Escalate

二十六、最终建议的 v1 故障矩阵
我不会把第一版限制在“20 个故障”，建议做成 24~30 个，但不是为了凑数字。
例如：
故障族场景Processprocess crashedProcessprocess killedProcessstale processPortport occupied by managed PIDPortport occupied by unknown PIDHTTPtimeoutHTTP500HTTP503Startupconfig missingStartupconfig malformedStartupdependency unavailableDBDB lockedDBintegrity failureStoragedisk > 90%Storagedisk > 95%Logsexcessive log growthRestartstartup loopRestartrestart timeoutBrowserCDP unavailableBrowserprocess alive but endpoint deadHermesgateway process alive but unhealthyMixedHTTP 503 + DB lockMixedprocess alive + port alive + health failBaselineeverything healthy
其中最重要的不是数量，而是：

至少加入 5~8 个“不能直接通过 if/else 一步得到答案”的 mixed cases。

这才是 LLM 的战场。

二十七、最终验收标准，我会这样改
不要只看：
修复成功率
增加三个真正有价值的指标：
1. Unsafe Remediation Rate
Agent 执行了不该执行的动作
/
总修复次数
这个甚至应该是：

必须为 0


2. LLM Incremental Value
LLM 系统在未知/混合故障上的表现
-
Rule-only baseline 表现
这个指标直接回答：

“为什么要 AI？”


3. Verification Escape Rate
也就是：

明明没有真正恢复，却被 Agent 判定为 recovered。

这个应该接近：

0%

这三个指标，比单纯增加 MTTR 更能体现你这个项目的思想。

二十八、我给这份评审稿的最终修改结论
保留
全部保留：


本地服务场景


Probe / LLM / Executor / Verifier 分离


状态机


白名单动作


幂等


回滚


人工升级


故障注入


holdout


结构化日志


CLI


三服务


这些都是正确方向。
必须修改
我认为有 6 项是 P0：
1. 增加 Policy Engine

LLM 只能提出候选动作，不能拥有执行权限。

2. 增加 Rule-only Baseline

用实验回答 LLM 是否真的有价值。

3. 把 8:2 train/holdout 改成 development / blind evaluation

这是系统工程，不要假装在训练模型。

4. 增加 cooldown + quarantine + retry budget

防止自愈系统变成故障放大器。

5. 数据库修复改为“安全检测 + 有限 remediation + escalation”

不要让 v1 自动重构数据库。

6. 引入 Incident Model + per-service repair lock

让状态机从“demo FSM”升级成真正可以追踪故障生命周期的系统。


二十九、你这项目真正最值钱的一句话
我甚至建议 README 首页直接写：

The LLM may propose a repair, but only deterministic policy allows it, and only a deterministic verifier can declare recovery.

中文就是：

LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器可以宣布恢复。

这句话基本就是你整个项目的灵魂。
它同时回答了：


为什么需要 LLM


为什么不能让 LLM 直接执行


为什么需要 Executor


为什么需要 Verifier


为什么需要 State Machine


为什么这是 Agent 而不是 watchdog


为什么这是一个安全的 Agent



最终评级
维度当前调整后工程完整性8/109.2/10Agent 含金量7.5/109/10SRE 味道8.5/109.3/10安全设计7/109.2/10可测试性8.5/109.5/10面试可讲性9/109.5/10作品集竞争力8/109/10+
结论：值得做，而且我建议把它作为你三个项目里的“收官项目”。
但不要把目标定成“做一个会自动重启服务的 Agent”，而要把目标定成：

做一个能够在严格安全边界内自主完成 Incident Lifecycle 的 LLM-assisted Self-Healing Orchestrator。

这两个定位，面试含金量不是一个级别。
