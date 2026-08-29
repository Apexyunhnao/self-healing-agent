# Claude Code 指令 05 —— 阶段 4：评估体系（run_eval.py + 修复语义调整）

> 复制下面「指令正文」给 Claude Code（终端版）。本任务由羔丸用 CLI 后台执行。

---

## 指令正文

**背景**：我们在做「Local Service Self-Healing Agent」（E:\Projects\self-healing-agent）。阶段 1（mock 环境+30 场景故障矩阵）、阶段 2（probe/executor/verifier）、阶段 3（state_machine/policy/rule_diagnoser/llm_diagnoser/orchestrator）已完成，68 单测全过。现在是**阶段 4：评估体系**——故障注入自动评估 + 指标报表。这是项目的"分水岭"：没有成功率报表的作品只是 demo。

**本任务两件事**：

## A. 修复 http_500 修复语义（让状态型故障可被 restart 修复）

现状问题：http_500 是状态文件驱动故障（mockenv/state/mock_svc.json 的 modes 含 http_500），mock_server 每次请求实时读状态文件。restart 只换进程不清状态文件 → 新进程仍返回 500 → 修复无效（demo 幕2/幕3 已展示）。

真实语义对照：HTTP 500 通常是应用进程内存状态坏了，**重启进程 = 清内存状态 = 500 消失**。mock 应该模拟这个。

**改动**：
1. `selfheal\executor.py` 的 restart 动作：如果 service_config 里有 `state_file` 字段，restart 前先删除该文件（清运行时状态，模拟"重启清内存"）。state_file 路径支持 ${MOCK_PORT} 变量替换。
2. `config\service.yaml` 的 mock_svc 加：`state_file: "mockenv/state/mock_svc.json"`
3. `selfheal\rule_diagnoser.py`：http_error 的置信度从 0.6 提到 **0.8**（restart 语义增强后，状态型故障 restart 可修复，置信度合理升高）
4. 更新相关测试（test_executor 加 restart 清 state_file 的用例；test_rule_diagnoser 更新 http_error 置信度断言）

## B. 评估框架 `eval\run_eval.py`

**功能**：
1. 读 `mockenv\scenarios\fault_matrix.json`（30 场景：24 单一 + 5 mixed + 1 baseline）
2. 8:2 切分（固定种子 42）：开发集 24 场景 / 盲评集 6 场景（**开发期不跑盲评**，最终验收才跑）
3. 对每个场景执行评估循环：
   - 起 mock（MOCK_PORT 用场景无关的固定 18080，先 cleanup 清残留）
   - fault_injector inject <scenario_id>
   - Orchestrator.run_once（diagnoser 可配：rule 模式 / llm 模式）
   - 记录：检测到（探针 ok=False）/ 最终状态 / 修复动作 / 状态轨迹 / 耗时
   - fault_injector recover + cleanup 清残留
4. **行为正确性判断**（每场景）：
   - 最终 healthy → repaired（修复成功）
   - 最终 awaiting_human / escalated → handled（正确处理：策略门拒绝或预算耗尽升级合理）
   - 探针没发现故障 → missed（检测失败）
   - 执行了不该执行的动作（如 rag_qa 被 restart、杀了未知进程）→ unsafe（红线）
   - 最终 recovered 但服务实际仍故障 → escape（验证器被骗）
5. **指标报表**（严格按 requirements.md 口径，数字必须能换算回整数）：
   - 检测率 = 被探针发现故障数 / 29（不含 baseline）
   - 修复成功率 = 回到 healthy 数 / 29
   - 正确处理率 = (repaired + handled) / 29
   - Unsafe Remediation Rate = unsafe 次数 / 修复尝试总次数（**必须为 0**）
   - Verification Escape Rate = escape 次数 / recovered 判定总数（**必须为 0**）
   - 误报率 = baseline 场景误触发修复数 / 1（**必须 0**）
   - MTTR = 修复成功场景从注入到 healthy 的秒数（均值+中位数）
   - Mixed 场景单独统计（LLM Incremental Value 用）
6. **Rule vs LLM 对比**：
   - `--diagnoser rule`（默认）：只用 rule_diagnoser（确定性 baseline）
   - `--diagnoser llm`：llm_diagnoser + rule fallback（无 DEEPSEEK_API_KEY 时自动降级，效果同 rule）
   - 两种模式跑同一开发集，报表里对比 Mixed 场景修复成功率 → LLM Incremental Value = LLM mixed 成功率 - Rule mixed 成功率
7. **命令行接口**：
   - `python eval\run_eval.py --dev`：跑开发集 24 场景（默认）
   - `python eval\run_eval.py --diagnoser llm`：LLM 模式
   - `python eval\run_eval.py --final`：全量 30 场景（含盲评，验收用）
   - 输出：终端打印指标 + 写 `eval\eval_report.md`（含每场景明细表）
8. **报表自洽**：明细表条数能对上指标分母；模板里禁止写死数字（全部来自变量）

## 验证要求（全部跑通再报告）

1. `python -m unittest discover -s tests -v` 全过（含更新的）
2. `python eval\run_eval.py --dev`（Rule 模式，24 场景）→ 报表出来，数字自洽
3. 如果某场景行为不合理（比如该检测到的没检测到），说明原因——不要为了数字好看改判断标准
4. 环境无残留 mock 进程/注入标记/状态文件

## 完成后报告

1. 改动的文件清单（executor/rule_diagnoser/service.yaml/测试）
2. 单测结果（几个 pass）
3. run_eval.py --dev 的完整报表输出
4. 每个 FAIL/异常场景的原因分析（如果有）

## 注意

- 不填 DEEPSEEK_API_KEY 也能跑（LLM 自动降级 Rule），不要试图生成/伪造 key
- 只改上述文件，不动 mockenv\ 下已有的 mock_server/mock_db/fault_injector/verify_env（state_file 是 executor 消费的配置，不改 mock）
- httpx 一律 trust_env=False；subprocess 一律 encoding="utf-8", errors="replace", timeout
- .env 由用户手动补（harness 拦），评估里遇到 .env 缺失优雅降级
- 不提交 GitHub，不 git init；不用 rm -rf
- 缺包用：python -m pip install httpx pydantic pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple
