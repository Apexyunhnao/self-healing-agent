# 开发日志（dev-log）

> 记录开发难点、决策时刻、踩坑过程。面试讲"为什么这么做"时引用。

## 阶段 1：需求 + mock 环境（2026-08-29 白天）

- 从真实崩溃记录反推故障矩阵：先有需求（本机服务老挂），后有场景（35 个）。
- 设计故障注入器时定了一条底线：**注入全部在隔离 mock 环境，随机高位端口，绝不触碰真实服务**。这让"评估"和"演示真实监控"可以分开做。
- 难点：mixed（组合故障）场景怎么定义才算有意义——不是简单叠加，而是"两个独立根因同时存在"，例如 http_503 + db_lock。

## 阶段 2-3：确定性模块 + 编排层（2026-08-29 下午-晚上）

- 状态机 13 个状态，手写。LangGraph 不是不好，是这个项目状态有限且安全关键路径需要完全可控。
- **关键决策**：确定性核心禁止 import LLM。当时 ChatGPT 评审提醒：如果策略门能放行 LLM 的任意动作，Unsafe 就不可能为 0。这条红线定了之后，后面所有安全指标都有了解释。
- 难点：状态机的事件驱动 + orchestrator 的 tick 循环怎么配合。最后定为：状态机只负责状态转移（事件），orchestrator 负责每轮"读状态 → 做事 → 发事件"。

## 阶段 4：评估 + LLM 对照实验（2026-08-29 深夜）

- **最重要的实验**：Rule-only vs LLM(+fallback) 同一测试集对比。结果 LLM 增量 = +4pp 修复率（18/28 vs 17/28）、+0pp 正确处理率、+1.2s MTTR、成本 >0。
- 决策：推荐 Rule-only 运行，LLM 作为可选模块保留。**这不是失败，是评估驱动的架构决策**——用实验数据决定 LLM 该待在哪。
- 难点：为什么 LLM 反而更差？深挖发现：探针信息太粗（只有健康状态），LLM 看到的信息和 Rule 一样，而 LLM 置信度高 → 策略门放行更多 → escape 更多。**无增量时不要硬吹**，诚实标注。
- 踩坑：评估报告口径不统一（"7 个升级" vs "22-13=9"），AI 评审指出后被追着修。教训：**指标分母必须从报表变量读，不能模板写死**。

## 阶段 5：CLI + Dashboard + 真实服务监控（2026-08-30 凌晨）

- 交付：CLI（services/status/run/verify/incidents/audit/dashboard-data）+ 单文件 dashboard.html + rag_qa 只读监控。
- 难点：Windows 控制台 ANSI 乱码 → 用 [OK]/[FAIL] 前缀替代颜色。
- **踩了大坑（详见下节）**：环境问题花了大半夜，最后证明是"后台残留进程污染共享环境"。

## 阶段 5 大坑实录（2026-08-30 凌晨，重要）

### 坑 1：claude.exe 后台任务不退出，循环污染环境

症状：测试偶发失败、mock 起不来、mock_svc.conf 反复被写坏成 UTF-16、state 里突然出现注入标记。

排查过程（按时间）：
1. 手动起 mock 成功 → 说明代码没问题
2. unittest 里起 mock 失败 → 怀疑 wmic 杀进程
3. 发现 wmic CommandLine like 自匹配 → 修了
4. 还是失败 → 发现 conf 被写坏 → 怀疑注入残留
5. 反复清理仍复发 → 用 wmic 列出所有进程 → **发现 00:31 启动的 claude.exe -p 还活着**，在循环跑 recover + test_orchestrator，实时写 state/conf/audit

根因：昨晚用 `claude -p` 后台生成阶段 5 代码，**任务完成后进程没退出**，继续执行其 prompt 里的验证循环。

教训：**长任务用后台跑完必须确认进程退出；跑测试前先查残留进程**。已写进 skill。

### 坑 2：wmic CommandLine like 自匹配

`wmic process where "CommandLine like '%mock_server.py%'"` 会匹配到执行 wmic 的进程链自己（bash/cmd 的命令行里含这个字符串）→ taskkill 误杀调用方。

修复：where 条件加 `and CommandLine not like '%wmic%'`。

### 坑 3：venv python shim 吞 stderr

venv\Scripts\python.exe 是 uv shim，跑脚本时子进程 stderr 丢失（报错无输出，RC=1 但看不到原因）。调试用真实 python 路径（uv 的 cpython）能看到错误。

## 评审整改（2026-08-30 凌晨）

阶段 4 评审（stage4-review.md）+ 阶段 5 评审（stage5-review.md）双 AI 意见一致，按优先级整改：

1. P0 run --dry-run：只诊断 + 策略门判定，不执行——把"安全边界"变成可现场演示的剧本
2. P0 incident <id>：incident_id 关联 incidents/policy 两文件，回放完整决策链——审计从"日志"升级成"证据链"
3. P1 audit schema 统一（schema_version + incident_id）
4. P1 JSONL 轮转（5MB）+ 损坏行处理——回答"日志一直涨/写坏了怎么办"
5. P1 --json 错误 envelope（{"ok":false,"error":{...}} + 非 0 退出码）——CLI 可管道给脚本
6. P1 logs 命令（--tail）——排障闭环：incident → action → verifier → logs
7. P2 dashboard 降噪：默认 10 条 + 显示更多

整改后：127 个单测全过。

## 后续（阶段 6 之后）

- 给 LLM 更丰富输入（日志尾部/进程快照/配置 diff），在规则未覆盖场景重测 LLM 增量
- --runs N 重复运行增强统计可信度
- 部署示例 + 外部 watchdog
