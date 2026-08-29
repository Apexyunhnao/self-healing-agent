# Claude Code 指令 06 —— 阶段 4 迭代：探针增强 + 验证器多次采样

> 复制下面「指令正文」给 Claude Code（终端版）。本任务由羔丸用 CLI 后台执行。

---

## 指令正文

**背景**：我们在做「Local Service Self-Healing Agent」（E:\Projects\self-healing-agent）。阶段 1-3 完成，阶段 4 评估框架 run_eval.py 已跑出第一版报表。开发集 24 场景 Rule 模式：检测率 20/23、修复 13/23、Unsafe=0、Escape 1/14、误报 0。暴露 3 类硬伤，本任务修掉它们，让报表更接近红线目标（Escape=0、检测率↑）。

**硬伤与修复方案**：

## 1. 探针漏检（3 个 missed 场景）

- **port_occupy_04**：端口被"托管服务的 dup 实例"接管，/healthz 仍 200，HTTP 探针无感知。修复：`selfheal\probe.py` 的 ProcessProbe 增加 `expected_count` 参数（默认 1）——查同 pattern 的进程数，进程数 > expected_count → ok=False（status="process_duplicate"）
- **cpu_spike_22 / mem_leak_23**：独立 busy-loop 进程模拟资源占用，mock 健康端点无感知。修复：新增 `ResourceProbe`（process_name 或 pid，cpu_threshold_pct=80，mem_threshold_mb=200）——Windows 用 wmic 查目标进程 CPU 时间增量或工作集内存，超阈值 → ok=False（status="cpu_spike"/"mem_high"）；找不到进程 → ok=False（status="process_missing"）。注：wmic 的 CPU 计算用两次采样差值（如 1 秒间隔），内存用 WorkingSetSize

## 2. 验证器被骗（escape：mixed_29 间歇故障）

- 现状：verify_http 只验一次 200 就判 recovered，间歇故障在"好窗口"骗过验证器。修复：`selfheal\verifier.py` 的 verify_http 增加 `samples=3, sample_interval=0.5`——连续 3 次（间隔 0.5s）都 200 才算 ok；任何一次失败则重试（走原有指数退避逻辑）。VerifyResult 记录 attempts 和 samples
- 连带：`selfheal\orchestrator.py` 验证阶段用新的 verify（samples=3）

## 3. service.yaml 配置

- mock_svc 加 `process.expected_count: 1`（供 ProcessProbe 用）
- mock_svc 加 `resources: {cpu_threshold_pct: 80, mem_threshold_mb: 200}`（供 ResourceProbe 用）

## 4. 评估脚本集成

- `eval\run_eval.py` 的探针构建逻辑：从 service.yaml 读 expected_count 和 resources，构建 ProcessProbe（带 expected_count）+ ResourceProbe（如果配置了）；Orchestrator tick 的探针列表包含 ResourceProbe
- 确保 cpu_spike_22/mem_leak_23 场景的注入方式（fault_injector 起独立 busy-loop 进程）能被 ResourceProbe 感知——busy-loop 进程的进程名要能匹配（查 fault_injector 怎么起的，可能用 python busy loop 脚本；如果资源探针按 process pattern 查不到独立进程，允许 ResourceProbe 按"总 CPU/内存"兜底或按注入器写入的 state 文件判定——优先按进程匹配，不行再看 state 文件）
- **注意**：ResourceProbe 对 cpu_spike/mem_leak 的判定如果依赖 state 文件，这属于"探针读注入痕迹"，测试场景可接受（fault-matrix 里 cpu_spike/mem_leak 本来就是观察类场景，expected 是 escalate）

## 5. 测试更新

- `tests\test_probe.py`：ProcessProbe expected_count 用例（起 2 个同名进程 → duplicate）
- `tests\test_verifier.py`：verify_http samples 用例（间歇 503/200 交替 → 连续 3 次 200 才 ok；中间有 503 → 失败）
- 全量单测保持通过

## 验证要求

1. `python -m unittest discover -s tests -v` 全过
2. `python eval\run_eval.py --dev`（Rule 模式）重跑 → 报表改善：检测率 ≥ 21/23（至少 cpu_spike/mem_leak 检测到）、Escape = 0
3. 环境无残留（注入标记 0、状态文件 0、mock 进程无残留）

## 完成后报告

1. 改动文件清单
2. 单测结果
3. 新版 Rule 报表指标（检测率/修复/正确率/Unsafe/Escape/误报/Mixed）
4. cpu_spike/mem_leak 场景的处理方式说明（按进程匹配还是 state 文件）

## 注意

- 不动 mockenv\ 下 mock_server/mock_db/fault_injector/verify_env 已有文件（ResourceProbe 是新增，不依赖 mock 改动）
- httpx 一律 trust_env=False；subprocess 一律 encoding="utf-8", errors="replace", timeout
- 不提交 GitHub，不 git init；不用 rm -rf
- 缺包：python -m pip install httpx pydantic pyyaml psutil -i https://pypi.tuna.tsinghua.edu.cn/simple（psutil 如果 Windows 装不上就用 wmic 方案，不要硬装）
