# Claude Code 指令 01 —— 阶段 1：mock 测试环境 + 故障注入器 + 故障矩阵

> 复制下面「指令正文」全部内容，粘贴给 Claude Code 执行。不要手动执行任何命令。

---

## 指令正文

**背景**：我们在做求职作品集收官项目「Local Service Self-Healing Agent（本地服务自愈智能体）」。它是一个自愈编排器：监控本机服务，故障时由 LLM/规则诊断根因，经策略门校验后执行白名单修复动作，由确定性验证器证明修复成功。文档已定稿：
- E:\Projects\self-healing-agent\requirements.md（需求与指标）
- E:\Projects\self-healing-agent\docs\fault-matrix-design.md（故障矩阵设计，30 场景清单，必须严格遵守）
- E:\Projects\self-healing-agent\CLAUDE.md（项目架构与安全红线，必须遵守）

当前是阶段 1：搭建**隔离的 mock 测试环境**。所有故障注入评估都在 mock 环境跑（随机高位端口），绝对不碰真实服务。本指令只做 mockenv 部分，不实现核心逻辑（探针/状态机等在后续指令做）。

**任务**（工作目录 E:\Projects\self-healing-agent）：

1. 创建目录结构：`mockenv\` 和 `mockenv\scenarios\`

2. 实现 `mockenv\mock_server.py`：
   - 基于 Python 标准库 http.server 或 FastAPI 的 HTTP mock 服务，作为**子进程**启动（subprocess.Popen）
   - 支持命令行参数：`--port`（随机高位端口 10000-60000）、`--name`（服务名，用于 PID 文件命名）
   - 启动时写 PID 文件到 `mockenv\run\<name>.pid`，记录进程 PID
   - 提供 `/healthz` 端点返回 200 JSON `{"status":"ok"}`
   - 可模拟故障模式（通过状态文件控制，状态文件在 `mockenv\state\<name>.json`）：
     - `http_500`：/healthz 返回 500
     - `http_503`：/healthz 返回 503
     - `timeout`：/healthz 挂起 30 秒不响应
     - `config_missing`：模拟配置文件被删（启动时检查一个 config 文件，不存在则启动失败退出）
     - `config_malformed`：模拟配置文件损坏（启动时解析失败退出）
     - `startup_loop`：启动后立即崩溃退出（反复重启也立即崩）
     - `startup_slow`：启动 30 秒后才监听端口（模拟启动超时场景）
   - 支持 `--daemon` 模式：启动后脱离终端继续跑（供注入器调用）

3. 实现 `mockenv\mock_db.py`：
   - 创建 SQLite 数据库文件 `mockenv\state\mock_svc.db`（带一张表 `kv(key TEXT, value TEXT)`）
   - 支持 `--lock`：对 DB 文件加独占锁（模拟 DB locked，用 `fcntl` 或 Windows 等价方案打开文件不释放）
   - 支持 `--corrupt`：向 DB 文件写入损坏字节（模拟 integrity failure）

4. 实现 `mockenv\fault_injector.py`（故障注入器）：
   - 读取 `mockenv\scenarios\fault_matrix.json`
   - 支持子命令：
     - `inject <scenario_id>`：执行该场景的注入动作
     - `recover <scenario_id>`：恢复该场景（清理注入痕迹，恢复 mock 可运行）
     - `--dry-run`：只打印将要执行的动作，不真正执行
     - `list`：列出所有场景（id + name + mixed 标记）
   - 注入方法实现（对应 fault-matrix-design.md 的 inject.method）：
     - `kill_pid`：kill mock 服务进程（读 PID 文件）
     - `occupy_port`：起一个假进程占住 mock 端口（模拟端口被占）
     - `http_5xx`：写状态文件让 mock 返回 500/503
     - `http_timeout`：写状态文件让 mock 挂起
     - `config_missing` / `config_malformed`：删除/写坏 mock 的 config 文件
     - `db_corrupt` / `db_lock`：调用 mock_db.py 对应模式
     - `disk_full_sim`：写一个大文件到 `mockenv\state\` 模拟磁盘占用（默认 100MB，可配）
     - `log_flood`：向 mock 日志目录写入大量日志行（默认 10 万行）
     - `startup_loop`：写状态文件让 mock 进入启动即崩模式
     - `health_fail`：写状态文件让 /healthz 返回 503（与 http_5xx 类似但语义不同）
     - `none`：不注入（baseline 场景）
   - 注入前必须检查目标是否存在，注入后写 `mockenv\state\injected_<scenario_id>.json` 标记文件（记录注入时间+动作），recover 时按标记清理
   - 所有外部命令设超时（默认 15 秒）

5. 生成 `mockenv\scenarios\fault_matrix.json`：
   - **必须正好 30 个场景**，严格按 docs/fault-matrix-design.md 第 2 节的场景清单（24 单一 + 5 mixed + 1 baseline），一个不差
   - schema 严格按 docs/fault-matrix-design.md 第 1 节：scenario_id / family / name / mixed / severity / inject{method,target,params} / expected{detect_probe,state_path,repair_action,verifier,llm_needed} / notes
   - mixed 场景（mixed_25 到 mixed_29）的 llm_needed 必须为 true，inject 里写清楚组合注入动作（如 mixed_25 = http_5xx + db_lock 两个动作）
   - baseline_30 的 inject.method 为 "none"
   - 写完后用 python json.load 验证可解析

6. **验证**（写一个 `mockenv\verify_env.py` 做自动验证）：
   - 启动 mock_server.py --daemon → 检查 PID 文件存在 + /healthz 返回 200
   - 注入 proc_crash_01（kill_pid）→ 验证 mock 进程已死 → recover → 再启动成功
   - 注入 http_500_06（http_5xx）→ /healthz 返回 500 → recover → /healthz 返回 200
   - 注入 db_lock_12（db_lock）→ 验证 DB 文件被锁（打开失败）→ recover → 可正常打开
   - 跑 fault_injector.py list → 确认输出 30 个场景
   - 打印验证结果报告

**安装依赖**（如缺）：`pip install pydantic httpx`（mock 环境不需要其他依赖）

**完成后报告**：
1. 创建的文件清单（路径）
2. fault_matrix.json 的场景数（必须 30，单一 24 / mixed 5 / baseline 1）
3. verify_env.py 验证结果（4 项检查 pass/fail）
4. fault_injector.py list 输出前 5 个场景示例

**注意**：
- 所有路径用 Windows 风格（E:\Projects\self-healing-agent\...），代码里用 pathlib 处理
- 不提交 GitHub，不初始化 git
- 本指令只做 mockenv 部分，不要提前实现 selfheal 核心包（那是后续指令）
- 如果 pip 安装失败，报告错误信息即可，不要换源硬试
