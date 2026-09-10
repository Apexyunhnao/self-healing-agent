# Claude Code 指令 03 —— 阶段 2：确定性核心模块（probe / executor / verifier）

> 复制下面「指令正文」全部内容给 Claude Code（终端版）。本任务由开发者在 CLI 后台执行，只需看结果。

---

## 指令正文

**背景**：我们在做「Local Service Self-Healing Agent」项目（E:\Projects\self-healing-agent）。阶段 1 已完成：mockenv\ 测试环境（mock_server.py / mock_db.py / fault_injector.py / verify_env.py）验证 8/8 PASS，scenarios\fault_matrix.json 30 场景就绪。

现在是**阶段 2：实现核心包 selfheal\ 的三个确定性模块**。这是自愈系统的"手脚"：
- probe.py（探针）：感知服务状态（HTTP/进程/DB/磁盘），确定性
- executor.py（执行器）：执行白名单修复动作，确定性 + 幂等
- verifier.py（验证器）：证明修复真实生效，确定性 + 重试退避
这三个模块**全部确定性，禁止调用任何 LLM/API**。LLM 诊断是阶段 3 的事（diagnoser.py）。

**设计原则**（写代码时严格遵守）：
1. 每个模块一个 dataclass 返回结构：ProbeResult / ActionResult / VerifyResult
2. 统一接口：check() / execute() / verify()
3. 幂等：重复执行无副作用（重启前先查进程状态，已死不重复杀）
4. 所有 subprocess.run 必须带 `encoding="utf-8", errors="replace"`
5. **所有 httpx 请求必须 `trust_env=False`**（Windows 系统代理 V2RayN 会拦截 localhost → 503，阶段 1 已踩坑）
6. Windows 路径用 pathlib，进程操作兼容 Windows（tasklist/taskkill/wmic）
7. 危险操作（kill 进程）只针对 service.yaml 声明的服务，绝不杀未知进程

**任务**：

1. 创建 `selfheal\__init__.py`（空或版本号）

2. `selfheal\probe.py`：
   - `@dataclass ProbeResult: ok: bool, status: str, detail: str, latency_ms: float`
   - `class HTTPProbe(url, timeout=5)`：httpx GET（trust_env=False），返回 ProbeResult；网络错误 → ok=False
   - `class ProcessProbe(name=None, pid_file=None)`：Windows 用 tasklist 查进程名或 pid 文件；进程存在且存活 → ok
   - `class DBProbe(path)`：sqlite3 打开 + PRAGMA integrity_check == "ok"
   - `class DiskProbe(path, threshold_pct=90)`：磁盘剩余空间，used > threshold → ok=False
   - 统一 `check() -> ProbeResult`

3. `selfheal\executor.py`：
   - `@dataclass ActionResult: ok: bool, action: str, detail: str`
   - 白名单动作（都从 service.yaml 读配置）：
     - `restart(service)`：幂等——先 ProcessProbe 检查，活着才 kill；启动用 service 的 start_command（subprocess 起子进程，DETACHED）；启动后不等就绪（就绪由 verifier 管）
     - `kill_stale(service)`：wmic 按命令行 pattern 杀残留进程
     - `cleanup_logs(service)`：删除 log_dir 下超过 size 阈值的文件
     - `git_revert_config(service)`：在 config_dir（git 仓库）执行 git reset --hard HEAD~1 或 git revert，失败返回 ok=False
     - `escalate(service, reason)`：把升级人工事件追加到 audit log（JSON 行），ok=True
   - `execute(action_name, service_config) -> ActionResult`

4. `selfheal\verifier.py`：
   - `@dataclass VerifyResult: ok: bool, attempts: int, detail: str`
   - `verify_http(url, timeout=5, retries=3, backoff=1.0)`：指数退避（1s/2s/4s），httpx trust_env=False，200 → ok；记录 attempts
   - `verify_process(name)`：进程存活
   - `verify_db(path)`：DB 可打开 + integrity
   - 统一 `verify() -> VerifyResult`

5. `config\service.yaml`：
   ```yaml
   services:
     mock_svc:
       display: Mock 测试服务
       probe:
         type: http
         url: "http://127.0.0.1:${MOCK_PORT}/healthz"
         timeout: 5
       process:
         pattern: "mock_server.py"      # 进程匹配（wmic CommandLine）
       repair:
         actions: [restart, kill_stale, cleanup_logs, escalate]
       verifier:
         type: http
         url: "http://127.0.0.1:${MOCK_PORT}/healthz"
         retries: 3
       start_command: ["python", "mockenv/mock_server.py", "--port", "${MOCK_PORT}", "--name", "mock_svc", "--daemon"]
       log_dir: "mockenv/state"
       config_dir: null
     rag_qa:
       display: RAG 知识库（真实服务，只读探针）
       probe:
         type: http
         url: "http://127.0.0.1:8000/healthz"
         timeout: 5
       process:
         pattern: "uvicorn"
       repair:
         actions: [escalate]           # 真实服务 v1 只升级人工，不自动重启
       verifier:
         type: http
         url: "http://127.0.0.1:8000/healthz"
         retries: 2
       start_command: null
       log_dir: null
       config_dir: null
   ```
   - 支持 `${MOCK_PORT}` 环境变量替换：写一个 load_services(yaml_path, env) 函数，把 ${VAR} 替换为 env 里的值（默认 mock 端口用 18080 之类测试端口，运行时由调用方传）

6. `tests\` 单测（unittest，不依赖 pytest）：
   - `tests\test_probe.py`：起一个 mock_server → HTTPProbe ok；停掉 → fail；DBProbe 正常路径
   - `tests\test_executor.py`：restart 幂等（连续两次 restart 都成功且不报错）
   - `tests\test_verifier.py`：verify_http 重试（先不可用后恢复 → ok，attempts>1）
   - 运行：`python -m unittest discover -s tests -v`

7. **集成闭环验证**（写一个 `demo_cycle.py` 放项目根，演示探针→故障→修复→验证全链路）：
   - 起 mock_svc（MOCK_PORT=18080）→ HTTPProbe ok
   - fault_injector inject http_500_06 → HTTPProbe fail（500）
   - executor restart(mock_svc) → verifier verify_http 200 ok
   - 打印每一步结果，最后清理（recover + 杀进程）
   - 运行：`python demo_cycle.py`

**完成后报告**：
1. 文件清单（selfheal\ 和 config\ 和 tests\）
2. 单测结果：几个测试全过（python -m unittest discover -s tests -v 的输出）
3. demo_cycle.py 完整输出（探针→故障→修复→验证闭环）
4. 如果集成闭环失败，说明卡在哪一步

**注意**：
- 不要实现 state_machine.py / diagnoser.py / policy.py / orchestrator.py（阶段 3 再做）
- 不要动 mockenv\ 下已有的文件
- 不提交 GitHub，不初始化 git
- 不要使用 rm -rf 类命令
- 如果 pip 缺包（httpx/pydantic/yaml），用 `python -m pip install httpx pydantic pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple` 装
