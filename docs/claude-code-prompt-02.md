# Claude Code 指令 02 —— 修复 verify_env.py 的 Windows 兼容 bug

> 复制下面「指令正文」全部内容，粘贴给 Claude Code（终端版：先运行 claude 进入交互，再粘贴）。

---

## 指令正文

**背景**：我们在做「Local Service Self-Healing Agent」项目（E:\Projects\self-healing-agent）。阶段 1 的 mock 环境代码已经写完（mockenv\ 下：mock_server.py、mock_db.py、fault_injector.py、verify_env.py、scenarios\fault_matrix.json 30 场景）。但 verify_env.py 在中文 Windows 上跑挂了，3 个 bug 都是 Windows 兼容问题，需要修复后重跑验证。

**已知 bug（根因已定位）**：
1. **UnicodeDecodeError**：subprocess.run 读子进程输出时按 UTF-8 解码，但中文 Windows 的子进程输出（tasklist 等）含 GBK 字节，解码失败。报错：`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd0`
2. **is_alive() 的 out.stdout 是 None**：这是 bug 1 的连带——解码失败后 subprocess.run 的 stdout 缓冲没填充，返回 None，`return f'"{pid}"' in out.stdout` 报 `TypeError: argument of type 'NoneType' is not iterable`
3. **mock 启动检查 FAIL**：也是 bug 1 连带（wait_mock → is_alive 崩了，导致 A 项判 FAIL；pid 文件实际已生成，说明 mock_server 能起）

**修复任务**：

1. 修改 `mockenv\verify_env.py`、`mockenv\fault_injector.py`、`mockenv\mock_server.py` 三个文件里**所有** subprocess.run 调用（包括 run_script、is_alive、kill_pid、注入器里的调用），统一加编码容错参数：
   ```python
   subprocess.run([...], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=...)
   ```
   errors="replace" 保证遇到 GBK 字节不崩溃，用替换符顶替。注意：编码参数要加在 text=True 的位置（text=True 模式才生效）。

2. 修复后**验证 mock_server.py 的 --daemon 模式**：
   - 运行 `python mockenv\mock_server.py --port <随机高位端口> --name daemon_test --daemon`
   - 确认进程能脱离终端独立运行（DETACHED_PROCESS 参数有效）、PID 文件写入正确
   - 然后 taskkill 杀掉它（用 taskkill /PID <pid> /F /T）

3. **重跑完整验证**：`python mockenv\verify_env.py`
   - 期望 8 项全 PASS（A、B1、B2、C1、C2、D1、D2、E）
   - E 项必须显示 30 场景（单一 24 / mixed 5 / baseline 1）

4. **顺手检查**：fault_injector.py list 输出格式（verify_env.py E 项靠它解析，如果 list 输出格式变了导致 E 判 FAIL，调整 verify_env.py 的解析逻辑或 list 格式，让 E 项能过）

**安装依赖**（如缺）：`pip install pydantic httpx`

**完成后报告**：
1. 改了哪几个文件、改了几处 subprocess.run
2. daemon 模式验证结果（能脱离终端运行？）
3. verify_env.py 完整输出（8 项 PASS/FAIL 列表 + 通过数）
4. 如果还有 FAIL，说明原因和你的修复思路

**注意**：
- 所有路径用 Windows 风格，代码里用 pathlib
- 只改 mockenv\ 下的三个脚本，不要动 scenarios\fault_matrix.json（30 场景已验证正确）
- 不提交 GitHub，不初始化 git
- 不要使用 rm -rf 类命令（权限被 deny）；清理临时文件用 python 的 pathlib
