# demo_new_features.py — 评审优化后新功能演示
import json, os, subprocess, sys, time
sys.path.insert(0, r'E:\Projects\self-healing-agent\tests')
from common import cleanup_state, free_port, kill_mock_processes, start_mock, wait_healthz

BASE = r'E:\Projects\self-healing-agent'
os.chdir(BASE)
PY = sys.executable
env = {**os.environ, 'SELFHEAL_NO_LLM': '1', 'PYTHONIOENCODING': 'utf-8'}

def cli(*args, timeout=60):
    r = subprocess.run([PY, '-m', 'selfheal.cli', *args], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=timeout, env=env, cwd=BASE)
    return r

def log(msg):
    print(msg, flush=True)

log('===== 评审优化新功能演示 =====')

# 1. dry-run 健康服务（演示策略门判定）
kill_mock_processes(); cleanup_state()
port = free_port()
env['MOCK_PORT'] = str(port)
start_mock(port)
assert wait_healthz(port, timeout=10)
r = cli('run', 'mock_svc', '--dry-run')
log('[1] run --dry-run（健康服务）:\n%s' % r.stdout)

# 2. 注入故障 → dry-run 显示 DENY/ALLOW
subprocess.run([PY, 'mockenv/fault_injector.py', 'inject', 'proc_crash_01'],
               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, cwd=BASE)
r = cli('run', 'mock_svc', '--dry-run')
log('[2] run --dry-run（注入崩溃后）:\n%s' % r.stdout)

# 3. 真实 run 修复（产生带 incident_id 的 policy 审计）
r = cli('run', 'mock_svc', '--json')
data = json.loads(r.stdout)
inc_id = data.get('incident_id', '')
log('[3] run mock_svc → incident_id=%s state=%s' % (inc_id[:8], data['final_state']))

# 4. incident show 回放决策链
r = cli('incident', inc_id)
log('[4] incident show %s:\n%s' % (inc_id[:8], r.stdout))

# 5. logs 命令
r = cli('logs', 'mock_svc', '--tail', '5')
log('[5] logs mock_svc --tail 5:\n%s' % r.stdout)

# 6. 错误 envelope（--json）
r = cli('status', 'nope', '--json')
log('[6] status nope --json → RC=%s STDERR=%s' % (r.returncode, r.stderr.strip()[:120]))

# 7. 重新生成 dashboard 数据
r = cli('dashboard-data')
log('[7] %s' % r.stdout.strip())

# 清理
subprocess.run([PY, 'mockenv/fault_injector.py', 'recover', '--all'],
               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, cwd=BASE)
kill_mock_processes(); cleanup_state()
log('===== 演示完成 =====')
