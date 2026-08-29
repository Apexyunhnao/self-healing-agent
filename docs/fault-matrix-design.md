# 故障矩阵设计（阶段 1 数据基线）

> 故障注入测试集 = 作品的评估地基。场景必须围绕真实故障精确设计，不能随机。
> 35 场景 = 单一 24 + Mixed 5 + Baseline 6（P0 整改后新增 5 个非故障基线：slow/warn/cpu/starting/multi_listen）。
> 开发集/盲评集 8:2（种子 42）在 run_eval.py 里切：开发集 29（23 故障 + 6 baseline），盲评集 6。

## 1. JSON Schema（每个场景一个对象）

```json
{
  "scenario_id": "proc_crash_01",
  "family": "process",
  "name": "process_crashed",
  "mixed": false,
  "severity": "critical",
  "inject": {
    "method": "kill_pid",
    "target": "mock_svc",
    "params": {}
  },
  "expected": {
    "detect_probe": "process_alive",
    "state_path": ["healthy", "degraded", "diagnosing", "policy_check", "repairing", "starting", "verifying", "recovered", "healthy"],
    "repair_action": "restart",
    "verifier": "http_200",
    "llm_needed": false
  },
  "notes": "kill mock 服务进程，探针应发现进程消失，修复=重启"
}
```

字段说明：
- `family`：故障族（process/port/http/startup/db/storage/logs/restart/browser/gateway/mixed/baseline）
- `mixed`：是否组合故障（LLM 战场标记）
- `inject.method`：kill_pid / occupy_port / http_5xx / config_missing / config_malformed / db_corrupt / disk_full_sim / log_flood / startup_loop / cdp_down / health_fail / none（baseline）
- `expected.state_path`：完整预期状态转移路径（评估时对比实际轨迹）
- `expected.llm_needed`：true = 该场景 Rule baseline 判不出，必须 LLM 才可能修复成功

## 2. 场景清单（30 个）

### 单一故障（24）

**Process（3）**
| id | name | 注入 | 预期动作 | llm_needed |
|---|---|---|---|---|
| proc_crash_01 | process_crashed | kill_pid(mock_svc) | restart | false |
| proc_kill_02 | process_killed_sigkill | kill -9 | restart | false |
| proc_stale_03 | stale_pid_file | 留僵尸 PID 文件、进程已死 | kill_stale + restart | false |

**Port（2）**
| port_occupy_04 | port_occupied_by_managed | 起第二个实例占同一端口 | kill_stale + restart | false |
| port_occupy_05 | port_occupied_unknown | 用假进程（非托管）占端口 | escalate（不杀未知进程） | false |

**HTTP（3）**
| http_500_06 | http_500 | mock 返回 500 | restart | false |
| http_503_07 | http_503 | mock 返回 503 | restart | false |
| http_timeout_08 | http_timeout | mock 挂起不响应 | kill + restart | false |

**Startup（3）**
| startup_missing_09 | config_missing | 删配置文件 | git_revert_config + restart | false |
| startup_malformed_10 | config_malformed | 配置文件写坏 | git_revert_config + restart | false |
| startup_dep_11 | dependency_unavailable | 依赖文件/端口不可用 | escalate | false |

**DB（2）**
| db_lock_12 | db_locked | mock DB 文件被独占锁 | restart（释放锁）+ 验证 | false |
| db_integrity_13 | db_integrity_failure | 写坏 DB 文件 | 检测→有限 remediation→escalate（不自动重构） | false |

**Storage（2）**
| disk_90_14 | disk_gt_90 | 模拟占用达 90% | cleanup_logs | false |
| disk_95_15 | disk_gt_95 | 模拟占用达 95% | cleanup_logs + escalate | false |

**Logs（1）**
| log_flood_16 | excessive_log_growth | mock 日志疯长 | cleanup_logs | false |

**Restart（2）**
| restart_loop_17 | startup_loop | mock 启动即崩 | 检测循环→escalate | false |
| restart_timeout_18 | restart_timeout | mock 启动超时（30s 不就绪） | starting 超时→escalate | false |

**Browser（2）**
| cdp_down_19 | cdp_unavailable | 调试端口 9224 不通 | restart | false |
| cdp_dead_20 | process_alive_endpoint_dead | 进程在但端点死 | kill + restart | false |

**Gateway（1）**
| gw_unhealthy_21 | process_alive_unhealthy | 进程在、HTTP 健康检查失败 | restart | false |

**其他（3）**
| cpu_spike_22 | cpu_spike | 模拟 CPU 100%（限速进程） | escalate（观察） | false |
| mem_leak_23 | memory_growth | 模拟内存持续增长 | escalate（观察） | false |
| clock_skew_24 | clock_skew | 模拟系统时间偏移（日志时间戳乱） | 检测→报告→escalate | false |

### Mixed（5）——LLM 的战场（llm_needed=true，Rule baseline 判不出或误判）

| id | 组合 | 为什么规则判不出 |
|---|---|---|
| mixed_25 | HTTP 503 + DB lock 同时 | 单看 503 会重启，但根因是 DB 锁，重启无效 |
| mixed_26 | process alive + port alive + health fail | 三层探针都"活"但服务不可用，需看日志定位 |
| mixed_27 | config 被部分修改 + 端口被未知进程占用 | 两个异常叠加，先修哪个、是否杀未知进程（安全边界） |
| mixed_28 | 服务健康但依赖服务挂了 | 主服务 200 但依赖不可用，探针层面无异常，需业务探针 |
| mixed_29 | 日志显示磁盘满警告 + HTTP 时好时坏 | 间歇性故障，根因在存储 |

### Baseline（1）

| baseline_30 | everything_healthy | 不注入任何故障，mock 全健康 | 预期：不触发任何修复（测误报率） |

## 3. 评估规则

- `llm_needed=false` 的场景：Rule Diagnoser 应独立修复成功（LLM 不参与或结果一致）
- `llm_needed=true` 的场景：Rule baseline 应失败或误判（对比素材），LLM 应修复成功——LLM Incremental Value 从这 5 个算
- baseline_30：整个评估期间不触发任何修复动作，否则误报率 > 0
- 每个场景跑完记录：实际 state_path 轨迹 + 修复动作 + 验证结果 + 耗时 → 与 expected 对比
- 盲评集（6 个，种子 42 从 30 个里切）：开发期禁止运行，最终验收用

## 4. mock 服务设计（供故障注入器使用）

- `mock_svc`：Python http.server 子进程，带 PID 文件、/healthz 端点、状态文件（可被注入器改坏）、配置目录（Git 管理）
- `mock_db`：SQLite 文件（可加独占锁/写坏）
- `mock_log`：日志目录（可疯长）
- `fake_unknown_proc`：非托管进程（占端口用，Agent 不应杀它）
- 端口：随机高位（10000+），避免撞真实服务
- 注入器 `fault_injector.py`：按场景 JSON 执行注入动作，运行后记录"已注入"标记，恢复时清理
