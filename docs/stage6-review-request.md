# 阶段 6 文档评审请求（README / interview-prep / tech_selection / dev-log）

请以资深技术面试官视角评审求职作品集文档。项目：Local Service Self-Healing Agent（CS 应届生求职 AI Agent 方向）。文档已写完，请你：
1. 挑出面试官会挑战/一眼看穿的漏洞（数字、口径、逻辑）
2. 评估 README 30 秒抓人效果
3. interview-prep 的答案有没有会被追问击穿的
4. 给 5 条以内最值得改的

## 核心文档（请全文阅读以下文件）

- E:\Projects\self-healing-agent\README.md
- E:\Projects\self-healing-agent\docs\interview-prep.md
- E:\Projects\self-healing-agent\docs\tech_selection.md

## 项目关键数字（与 eval_report.md 核对）

- 35 场景 = 30 故障 + 5 baseline（原需求写 24+5+6，实际矩阵 35，port_occupy_04 标 invalid 排除）
- 开发集 29（23 故障 + 6 baseline）：检测 22/22=100%、修复 13/22=59.1%、正确处理 22/22=100%、Unsafe 0/26、Escape 0/13、误报 0/6、MTTR 8.1s、Mixed 2/4
- 全量 30（含盲评 6）：检测 28/29=96.6%、正确处理 96.6%、Unsafe 0/34、Escape 0/17
- LLM 对照：修复 +0pp、正确处理 -5pp、MTTR +7.8s、成本 $0.003/60 调用 → 默认关闭 LLM
- 单测 127 个

## 评审重点

1. README 是否能让面试官 30 秒内抓住核心价值？
2. 数字口径有没有漏洞（比如 35 vs 30 vs 29、修复成功率 vs 正确处理率的解释）？
3. interview-prep 的 19 个问题答案，哪些会被面试官一句话击穿？
4. 技术选型文档的 trade-off 是否站得住？
5. 作为一个 AI Agent 方向求职作品，这个项目的差异化卖点是什么，文档有没有把它讲出来？
