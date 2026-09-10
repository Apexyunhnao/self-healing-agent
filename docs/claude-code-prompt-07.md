# Claude Code 指令 07 —— 阶段 4 评审整改（P0：口径/标记/重复运行/成本）

> 复制下面「指令正文」给 Claude Code（终端版）。本任务由开发者在 CLI 后台执行。

---

## 指令正文

**背景**：我们在做「Local Service Self-Healing Agent」（E:\Projects\self-healing-agent）。阶段 4 评估体系已跑通（开发集 Rule 95.7% 检测 / Unsafe=0 / Escape=0）。刚做完 DeepSeek+ChatGPT 双 AI 评审（docs\stage4-review.md），以下是 P0 整改项。**不动系统行为逻辑**，只改评估口径、报告、标记、统计。

**整改项**：

## 1. 口径统一（报告里数字要自洽，防面试官一眼看穿）

- 升级人工数量统一：开发集 23 故障 = 13 修复 + 9 升级 + 1 未检测。`eval\run_eval.py` 的明细/总览必须从这个分解推导，禁止写死数字
- Unsafe 分母解释：报告增加"修复尝试次数 vs 建议次数"——口径写"Unsafe = 违规执行动作数 / 修复执行次数"，同时报告"诊断建议总次数"（Rule/LLM 各多少），一句话解释 LLM 建议次数比 Rule 多的原因（LLM 模式重试时每轮重新诊断）
- MTTR 口径注明："只统计自动修复成功场景（回到 healthy），不含升级人工等待"，并加 P50/P90/P95

## 2. port_occupy_04 标记（Test Infrastructure Failure）

- fault_matrix.json 的 port_occupy_04 加字段：`"test_status": "invalid", "invalid_reason": "injector_noop", "excluded_from_metrics": true`
- run_eval.py 统计时排除 invalid 场景（不列入分子分母），但明细表保留显示（标注 invalid）
- 报表说明区分：System failure / Test-case failure / Environment failure

## 3. 重复运行开关（统计意义）

- run_eval.py 加 `--runs N` 参数（默认 1）：同一场景跑 N 次，指标按累计统计（如 23 故障 × 5 runs = 115 次执行）
- 报告加"累计口径"：Unsafe 0/115、Escape 0/65 之类（累计分母=每次运行的实际执行次数之和）
- 注：--runs 5 时每个场景跑 5 遍（注入→编排→验证→recover），耗时约 5 倍，可接受

## 4. 正常场景扩容（误报率统计意义）

- fault-matrix.json 增加 5 个 baseline 变体（family=baseline，inject.method=none，expected=不触发修复）：
  - baseline_slow_31：服务正常但响应慢（mock_server 加 slow 模式，/healthz 延迟 1s，探针 timeout 5s 不应判失败）
  - baseline_warn_32：服务正常但日志有 warning（写 warning 日志，不触发修复）
  - baseline_cpu_33：CPU 偏高但低于阈值（ResourceProbe 阈值 80%，实际 60%，不触发）
  - baseline_starting_34：服务启动中（刚启动 3s 内探针可能失败 1 次，不应触发 degraded——需要 orchestrator 容忍启动期单次失败）
  - baseline_multi_listen_35：端口多监听但服务正常（进程数=2 但 expected_count 配置为 2，不触发）
- mock_server.py 需要支持：slow 模式（延迟响应）、warning 日志模式。**注意**：mock_server.py 在 mockenv\ 下，本任务允许改它（加 slow/warning 模式），但不动已有故障模式
- 误报率指标分母变为 6（原 1 个 baseline + 新增 5 个）
- 开发集/盲评集切分调整：种子 42 重新切，保持盲评 6 个不变（新增 5 个 baseline 进开发集）

## 5. LLM 成本报告

- llm_diagnoser.py 记录每次调用：时间戳、输入 token、输出 token、耗时（追加到 audit\llm_calls.jsonl）
- run_eval.py LLM 模式报表增加：LLM 调用总次数、平均耗时、估算成本（DeepSeek v4-flash 单价，写死 $0.14/1M in 也行，注明是估算）
- 报表结论区自动生成："LLM: repair +0pp, correct handling +0pp, MTTR +Xs, cost >0 → 默认关闭（可选实验模块）"

## 6. 测试更新

- tests\ 新增/更新：baseline 场景 mock slow/warning 模式的单测；run_eval 统计排除 invalid 的测试
- 全量单测保持通过

## 验证要求

1. `python -m unittest discover -s tests -v` 全过
2. `python eval\run_eval.py --dev`：报表显示新口径（13修复+9升级+1未检测+invalid 标记），指标自洽
3. 快速验证新增 5 个 baseline 场景不误触发（误报率 0/6）
4. 环境无残留

## 完成后报告

1. 改动文件清单
2. 单测结果
3. --dev 新报表（含新增 baseline 指标、invalid 标记、LLM 成本区占位）
4. 5 个新 baseline 场景的验证结果（都不误触发？）

## 注意

- 不要动 orchestrator/state_machine 的修复逻辑（本次只改评估口径/报告/标记/mock 加 slow/warning 模式）
- httpx 一律 trust_env=False；subprocess 一律 encoding="utf-8", errors="replace", timeout
- 不提交 GitHub，不 git init；不用 rm -rf
- 缺包：python -m pip install httpx pydantic pyyaml psutil -i https://pypi.tuna.tsinghua.edu.cn/simple
