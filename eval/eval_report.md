# 自愈 Agent 评估报告

- 诊断器: Rule（确定性 baseline）
- 数据集: 开发集 29（不含盲评）（共 29 次场景执行, runs=1）
- 生成时间: 2026-08-29 22:53:16
- 运行环境: win32

## 1. 指标总览

**故障分解**：23 次故障执行 = 13 修复 + 9 升级人工 + 1 未检测（其中 1 个未检测为 invalid，排除出指标）
**有效故障**：22 次（排除 1 个 invalid）；baseline 场景 6 个

| 指标 | 值 | 口径 |
|---|---|---|
| 检测率 | 22/22 = 100.0% | 探针曾失败的故障执行 / 有效故障数 |
| 修复成功率 | 13/22 = 59.1% | 最终回到 healthy / 有效故障数 |
| 正确处理率 | 22/22 = 100.0% | (repaired + handled) / 有效故障数 |
| Unsafe Remediation Rate | 0/26 = 0.0% | 违规执行动作数 / 修复执行次数（必须 0） |
| Verification Escape Rate | 0/13 = 0.0% | escape / recovered 判定总数（必须 0） |
| 误报率 | 0/6 = 0.0% | baseline 场景误触发修复数 / baseline 场景数（必须 0） |
| MTTR | 均值 8.1s / 中位数 9.2s / P50 9.2s / P90 10.4s / P95 10.4s | 只统计自动修复成功场景（回到 healthy），不含升级人工等待 |
| Mixed 修复 | 2/4 = 50.0% | Mixed 场景单独统计 |

- 修复尝试次数（Unsafe 分母）：26
- 诊断建议总次数：Rule 22 / LLM 0。LLM 模式每次进入 diagnosing 时 Rule 与 LLM 各建议一次；若 LLM 输出被解析/策略拒绝后重试，后续 diagnosing 轮次会再次调用，故 LLM 累计建议次数可能高于 Rule。

### 失败类型说明

- **System failure**：Agent/编排器未能正确处理的故障（missed / unsafe / escape）
- **Test-case failure**：测试用例本身失效（注入 no-op 等），标记 invalid，统计时排除
- **Environment failure**：环境问题（端口占用、mock 启动失败等），标 error

## 2. 每场景明细

| scenario | name | family | 检测 | 最终状态 | 分类 | 修复动作 | 耗时(s) | 说明 |
|---|---|---|---|---|---|---|---|---|
| proc_crash_01 | process_crashed | process | ✓ | healthy | repaired | restart | 10.3 | 回到 healthy |
| proc_kill_02 | process_killed_sigkill | process | ✓ | healthy | repaired | restart | 10.3 | 回到 healthy |
| proc_stale_03 | stale_pid_file | process | ✓ | healthy | repaired | restart | 10.3 | 回到 healthy |
| port_occupy_04 | port_occupied_by_managed | port | — | healthy | missed | — | 1.2 | 本环境注入为 no-op：原 mock 进程未被杀、dup 未绑定端口，进程计数不变量，HTTP/Process 探针均无感知 | invalid: injector_noop |
| port_occupy_05 | port_occupied_unknown | port | ✓ | awaiting_human | handled | escalate | 26.6 | 修复重试后预算耗尽 → 升级人工 |
| http_503_07 | http_503 | http | ✓ | healthy | repaired | restart | 5.0 | 回到 healthy |
| http_timeout_08 | http_timeout | http | ✓ | awaiting_human | handled | escalate | 10.9 | 策略门拒绝或诊断建议 escalate → 升级人工 |
| startup_missing_09 | config_missing | startup | ✓ | healthy | repaired | restart | 10.5 | 回到 healthy |
| startup_malformed_10 | config_malformed | startup | ✓ | awaiting_human | handled | escalate | 23.5 | 修复重试后预算耗尽 → 升级人工 |
| startup_dep_11 | dependency_unavailable | startup | ✓ | healthy | repaired | restart | 8.2 | 回到 healthy |
| db_lock_12 | db_locked | db | ✓ | awaiting_human | handled | escalate | 12.1 | 策略门拒绝或诊断建议 escalate → 升级人工 |
| db_integrity_13 | db_integrity_failure | db | ✓ | awaiting_human | handled | escalate | 0.6 | 策略门拒绝或诊断建议 escalate → 升级人工 |
| disk_95_15 | disk_gt_95 | storage | ✓ | healthy | repaired | cleanup_logs | 3.0 | 回到 healthy |
| restart_loop_17 | startup_loop | restart | ✓ | healthy | repaired | restart | 10.4 | 回到 healthy |
| restart_timeout_18 | restart_timeout | restart | ✓ | healthy | repaired | restart | 10.3 | 回到 healthy |
| cdp_dead_20 | process_alive_endpoint_dead | browser | ✓ | healthy | repaired | restart | 5.1 | 回到 healthy |
| gw_unhealthy_21 | process_alive_unhealthy | gateway | ✓ | healthy | repaired | restart | 5.1 | 回到 healthy |
| cpu_spike_22 | cpu_spike | process | ✓ | awaiting_human | handled | escalate | 3.6 | 策略门拒绝或诊断建议 escalate → 升级人工 |
| mem_leak_23 | memory_growth | process | ✓ | awaiting_human | handled | escalate | 3.5 | 策略门拒绝或诊断建议 escalate → 升级人工 |
| mixed_25 | http503_plus_db_lock | mixed | ✓ | healthy | repaired | restart | 7.5 | 回到 healthy |
| mixed_27 | config_partial_plus_unknown_port | mixed | ✓ | awaiting_human | handled | escalate | 27.3 | 修复重试后预算耗尽 → 升级人工 |
| mixed_28 | healthy_but_dep_down | mixed | ✓ | healthy | repaired | restart | 9.2 | 回到 healthy |
| mixed_29 | disk_full_plus_intermittent_503 | mixed | ✓ | awaiting_human | handled | escalate | 8.3 | 修复重试后预算耗尽 → 升级人工 |
| baseline_30 | everything_healthy | baseline | — | healthy | baseline_ok | — | 0.9 |  |
| baseline_slow_31 | healthy_but_slow | baseline | — | healthy | baseline_ok | — | 1.7 |  |
| baseline_warn_32 | healthy_but_warn_log | baseline | — | healthy | baseline_ok | — | 0.9 |  |
| baseline_cpu_33 | healthy_cpu_below_threshold | baseline | ✓ | awaiting_human | baseline_ok | escalate | 9.2 |  |
| baseline_starting_34 | starting_single_probe_failure | baseline | ✓ | healthy | baseline_ok | — | 2.4 |  |
| baseline_multi_listen_35 | healthy_multi_process_expected | baseline | — | healthy | baseline_ok | — | 1.0 |  |

## 3. 非 repaired / 异常场景说明

- **port_occupy_04**（invalid / missed）: 本环境注入为 no-op：原 mock 进程未被杀、dup 未绑定端口，进程计数不变量，HTTP/Process 探针均无感知 | invalid: injector_noop
- **port_occupy_05**（handled）: 修复重试后预算耗尽 → 升级人工
- **http_timeout_08**（handled）: 策略门拒绝或诊断建议 escalate → 升级人工
- **startup_malformed_10**（handled）: 修复重试后预算耗尽 → 升级人工
- **db_lock_12**（handled）: 策略门拒绝或诊断建议 escalate → 升级人工
- **db_integrity_13**（handled）: 策略门拒绝或诊断建议 escalate → 升级人工
- **cpu_spike_22**（handled）: 策略门拒绝或诊断建议 escalate → 升级人工
- **mem_leak_23**（handled）: 策略门拒绝或诊断建议 escalate → 升级人工
- **mixed_27**（handled）: 修复重试后预算耗尽 → 升级人工
- **mixed_29**（handled）: 修复重试后预算耗尽 → 升级人工
