# Local Service Self-Healing Agent — 阶段 5 评审请求

请以资深 SRE/后端工程师视角评审这个"自愈 Agent"作品的**阶段 5（出口层）**设计。项目是黄文浩的求职作品集项目，会在面试中向面试官演示。你的任务：挑毛病、给改进建议，重点是"面试演示效果"和"工程严谨性"。

## 项目背景（阶段 1-4 已完成，已验证）

一个本地服务的自愈 Agent：监控 → 诊断 → 修复 → 验证 → 兜底。核心哲学：
**"LLM 可以提出修复建议，但只有确定性策略允许执行，只有确定性验证器可以宣布恢复。"**

- 阶段 1：mock 环境 + 35 场景故障矩阵（30 故障 + 5 baseline）
- 阶段 2：确定性模块 probe/executor/verifier + service.yaml
- 阶段 3：编排层（状态机/策略门/Rule+LLM 双诊断器/orchestrator）
- 阶段 4：评估体系。关键结果：开发集检测 100%/正确处理 100%/Unsafe 0/Escape 0/误报 0/MTTR 8.1s；盲评 6 场景无过拟合；LLM 增量评估显示当前信息下无增量（默认关闭，标为可选增强）
- 阶段 5（本次评审）：CLI + 审计日志查看 + Incident Dashboard + 真实服务只读监控

## 阶段 5 交付物

### 1. CLI（selfheal/cli.py，argparse，python -m selfheal.cli）

命令：
- `services`：列出 service.yaml 配置的服务
- `status [service]`：实时探针状态（HTTP 状态码/进程存活/资源），[OK]/[FAIL] 前缀（避免 Windows ANSI 乱码）
- `run <service>`：跑一次完整生命周期（探针→诊断→策略→执行→验证），打印状态路径和结果
- `verify <service>`：只跑确定性验证器
- `incidents [--last N]`：读 audit/incidents.jsonl，按时间倒序
- `audit [--last N]`：读 audit/policy.jsonl（策略门审计：服务/动作/置信度/允许拒绝/原因）
- `dashboard-data`：导出 dashboard_data.js（dashboard 数据源）
- 所有命令支持 --json 输出；try/except 友好报错（不裸 traceback）

关键设计：status 命令故意不做 expected_count 判重（避免 Windows venv shim 多进程误报），判重留给评估期探针。

### 2. dashboard.html（单文件，纯前端原生 JS+CSS，无 CDN）

- 深色主题，数据源降级：dashboard_data.js（file:// 双击可用）→ fetch audit/incidents.jsonl（HTTP 服务器）→ 无数据提示
- 展示：指标卡（从 eval/eval_report.md 提取检测率/修复成功率/Unsafe/Escape/误报/MTTR）+ Incident 列表（ID/服务/状态徽章/动作/耗时）+ 状态流（DEGRADED → DIAGNOSING → REPAIRING → VERIFYING → HEALTHY/ESCALATED 箭头渲染）
- 最多展示 50 条

### 3. 真实服务只读监控

- config/service.yaml 里 rag_qa（知识库 8000 端口）只允许 escalate，不允许自动 restart
- `status rag_qa` 在知识库未启动/端点异常时显示 [FAIL]——演示"真实服务监控"

## 验证结果（全部通过）

- 112 个 unittest 全过（阶段 1-5 全部回归）
- CLI 演示：services → status OK → 注入 proc_crash → status FAIL → run 修复（restart，3.0s，状态路径 healthy→degraded→diagnosing→policy_check→repairing→starting→verifying→recovered→healthy）→ status OK → verify OK → incidents/audit 正常
- rag_qa 真实服务：知识库在跑但 /healthz 404 → status 显示 [FAIL] + http_404

## 评审重点

1. **面试演示**：这套 CLI+dashboard 在面试官面前演示什么最有冲击力？有没有多余的东西？
2. **CLI 设计**：命令划分、输出格式、错误处理有什么问题？有没有该加没加的命令（比如 logs、metrics、config 查看）？
3. **dashboard**：单文件无 CDN 的限制下，信息呈现有什么缺陷？（比如状态流对非技术面试官是否直观）
4. **真实服务监控**：rag_qa 只读监控的设计（只 escalate 不自动重启）是亮点还是鸡肋？有没有更好的演示角度？
5. **工程严谨性**：审计日志（incidents.jsonl/policy.jsonl）的 schema、轮转、可回放性有什么问题？
6. **阶段 6 展望**：README/tech_selection/interview-prep 要怎么写才能把这套系统讲清楚？

请给 5-8 条具体、可执行的改进建议，按优先级排序，每条说明"为什么重要"和"怎么改"。
