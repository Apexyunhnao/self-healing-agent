# Claude Code 指令 08 —— 阶段 5：CLI + 审计查看 + Incident Dashboard + 真实服务只读监控

> 复制下面「指令正文」给 Claude Code（终端版）。本任务由羔丸用 CLI 后台执行。

---

## 指令正文

**背景**：我们在做「Local Service Self-Healing Agent」（E:\Projects\self-healing-agent）。阶段 1-4 完成：mock 环境 + 30 场景 + 确定性模块 + 编排层 + 评估体系（97 单测，开发集检测 100%/正确处理 100%/Unsafe 0/Escape 0，盲评无过拟合）。现在是**阶段 5：出口**——CLI 工具 + 审计日志查看 + 轻量 Incident Dashboard + 真实服务只读监控。目标是"能演示、能排查、能给别人看"。

**现有代码**（直接 import 复用）：selfheal\probe.py（HTTP/Process/DB/Disk/Resource 探针）、executor.py、verifier.py、state_machine.py、policy.py、rule_diagnoser.py、llm_diagnoser.py、orchestrator.py、config.py（load_services，${VAR} 替换）；config\service.yaml（mock_svc + rag_qa）；audit\ 下已有 policy.jsonl / incidents.jsonl / llm_calls.jsonl。

**任务**：

1. `selfheal\cli.py`（argparse，入口 `python -m selfheal.cli`）：
   - `status [service]`：列出所有服务（或指定）的实时探针状态——HTTP 状态码/进程存活/DB 完整/磁盘/资源，颜色标记 ok/fail（Windows 控制台可能不支持颜色，用 [OK]/[FAIL] 前缀替代，避免 ANSI 乱码）
   - `run <service>`：对该服务跑一次完整生命周期（探针→诊断→策略→执行→验证），打印状态路径和结果
   - `verify <service>`：只跑验证器（确定性证明当前是否健康）
   - `incidents [--last N]`：查看最近 N 条 Incident（读 audit\incidents.jsonl，按时间倒序，打印状态路径+动作+结果）
   - `audit [--last N]`：查看策略门审计（读 audit\policy.jsonl：服务/动作/置信度/允许还是拒绝/原因）
   - `services`：列出 service.yaml 里配置的服务
   - `--json` 可选：输出 JSON（给 dashboard/脚本用）
   - 所有命令都要 try/except 友好报错（服务没起/文件缺失时给中文提示，不裸 traceback）

2. `dashboard.html`（单文件，项目根）：轻量 Incident Dashboard
   - 纯前端（无构建，原生 JS + CSS），用户用浏览器打开就能看
   - 读取 `audit\incidents.jsonl` 和 `eval\eval_report.md`（如果存在）
   - 展示：最近 Incidents 列表（Incident #id、服务、最终状态、动作、耗时）+ 每个 Incident 的状态流（DEGRADED → DIAGNOSING → REPAIRING → VERIFYING → HEALTHY / ESCALATED，用箭头文本流渲染）
   - 顶部显示评估指标摘要（从 eval_report.md 提取：检测率/修复成功率/Unsafe/Escape/误报）
   - 深色主题，单文件无外部依赖（不引 CDN，国内可打开）
   - 说明：dashboard.html 是静态文件，需要先跑过 run_eval/CLI 才有数据；文件顶部注释写"用法：python -m selfheal.cli incidents --json 生成数据后刷新"

3. **真实服务只读监控**：
   - 确认 config\service.yaml 的 rag_qa 配置正确（HTTP 8000、只允许 escalate）
   - `python -m selfheal.cli status rag_qa` 在知识库服务没启动时应显示 [FAIL]（HTTP 不通）——这是演示"真实服务监控"的好场景，不需要启动知识库
   - Orchestrator 对 rag_qa 只走 escalate（策略门已保证），不自动 restart

4. `tests\test_cli.py`（unittest）：
   - status 命令在 mock_svc 起/停时输出 ok/fail
   - run 命令对 mock_svc 跑生命周期（起 mock → proc_crash → run → healthy）
   - incidents/audit 命令能读文件（空文件/有数据两种情况）
   - 子进程方式跑 CLI（python -m selfheal.cli status --json）验证输出可解析

**验证要求**（全部跑通再报告）：
1. `python -m unittest discover -s tests -v` 全过（含新 CLI 测试）
2. `python -m selfheal.cli services` 列出 mock_svc + rag_qa
3. mock 演示：起 mock_svc（MOCK_PORT=18080）→ `python -m selfheal.cli status mock_svc` 显示 [OK] → fault_injector inject proc_crash_01 → status 显示 [FAIL] → `python -m selfheal.cli run mock_svc` 修复回 [OK]
4. `python -m selfheal.cli status rag_qa`：知识库 8000 没启动时显示 [FAIL]（演示真实服务监控）
5. `python -m selfheal.cli incidents --last 5` 和 `audit --last 5` 能读文件输出
6. dashboard.html 能被浏览器打开（人工确认或至少文件结构完整）

**完成后报告**：
1. 文件清单（cli.py/dashboard.html/tests）
2. 单测结果
3. CLI 演示输出（status OK→FAIL→run 修复→OK 的完整过程）
4. rag_qa 真实服务 status 输出
5. incidents/audit 命令示例输出

**注意**：
- Windows 控制台中文输出注意编码（如果打印中文乱码，脚本开头加 sys.stdout.reconfigure(encoding="utf-8", errors="replace")）
- httpx 一律 trust_env=False；subprocess 一律 encoding="utf-8", errors="replace", timeout
- 不提交 GitHub，不 git init；不用 rm -rf
- 不要动 selfheal\ 已有模块的核心逻辑（只新增 cli.py，必要时小改 config 或 orchestrator 暴露接口）
- dashboard.html 不引外部 CDN（国内可打开）
