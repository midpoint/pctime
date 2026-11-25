# -*- coding: utf-8 -*-
"""纯逻辑单元测试：快捷键、端口校验、连点计数、静音状态机、数据库迁移、
登录限流、指令分流，以及守护进程停止（仅 Windows，会真起并结束一个子进程）。

不启动客户端、不显示窗口；除守护进程段（仅 Windows）外均可跨平台运行。

作者：野怼怼；https://www.bd7iwd.com
许可：MIT
"""
import json
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

# 必须在导入 core.config 之前指定数据目录，避免污染真实数据
# （PCTL_DATA_DIR 优先级高于 LOCALAPPDATA，两个都要指到临时目录）
_TMP_DATA = tempfile.mkdtemp(prefix='pctl_units_')
os.environ['LOCALAPPDATA'] = _TMP_DATA
os.environ['PCTL_DATA_DIR'] = _TMP_DATA
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config  # noqa: E402
from core import hotkey  # noqa: E402
from core.backdoor import ClickCounter  # noqa: E402
from core.audio import AudioMuter  # noqa: E402

ok = True


def check(name, cond, extra=''):
    global ok
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('  -> ' + str(extra)) if extra and not cond else ''))
    if not cond:
        ok = False


check('数据目录在临时目录内', str(config.DATA_DIR).startswith(tempfile.gettempdir()),
      config.DATA_DIR)


# ------------------------------------------------------------------ 快捷键

print('=== 快捷键解析 ===')
mods, vk = hotkey.parse_hotkey('Ctrl+Alt+M') or (0, 0)
check('Ctrl+Alt+M 修饰键', mods == (hotkey.MOD_CONTROL | hotkey.MOD_ALT), mods)
check('Ctrl+Alt+M 主键', vk == ord('M'), vk)
check('大小写/顺序规范化', hotkey.normalize_hotkey('alt+ctrl+m') == (True, 'Ctrl+Alt+M'),
      hotkey.normalize_hotkey('alt+ctrl+m'))
check('F12 支持', hotkey.normalize_hotkey('Ctrl+Shift+F12') == (True, 'Ctrl+Shift+F12'),
      hotkey.normalize_hotkey('Ctrl+Shift+F12'))
check('数字键支持', hotkey.normalize_hotkey('Win+Alt+9') == (True, 'Alt+Win+9'),
      hotkey.normalize_hotkey('Win+Alt+9'))
for bad in ('', 'M', 'Ctrl+Alt', 'Ctrl+Ctrl+M', 'Shift+M', 'Ctrl+Alt+ZZ', 'Ctrl+Alt+'):
    check(f'拒绝非法快捷键 {bad!r}', hotkey.normalize_hotkey(bad)[0] is False)


# ------------------------------------------------------------------ 端口

print('=== 端口校验 ===')
for bad, kw in ((80, '1024'), (6666, '不安全'), (65536, '1024')):
    good, msg = config.validate_port(bad)
    check(f'拒绝端口 {bad}', (not good) and kw in msg, msg)

s = socket.socket()
s.bind(('127.0.0.1', 0))
free_port = int(s.getsockname()[1])
s.close()
good, msg = config.validate_port(free_port)
check(f'接受空闲端口 {free_port}', good, msg)


# ------------------------------------------------------------------ 连点

print('=== 连点计数 ===')
c = ClickCounter(10, 6.0)
check('9 次不触发', not any(c.click(now=i * 0.1) for i in range(9)))
check('第 10 次触发', c.click(now=0.9) is True)


# ------------------------------------------------------------------ 静音

print('=== 静音状态机 ===')


class FakeBackend:
    def __init__(self):
        self.muted = False
        self.calls = []

    def get_mute(self):
        self.calls.append(('get', self.muted))
        return self.muted

    def set_mute(self, value):
        self.muted = bool(value)
        self.calls.append(('set', self.muted))
        return True


state_file = Path(tempfile.mkdtemp(prefix='pctl_audio_')) / 'audio.state'
b = FakeBackend()
a = AudioMuter(backend=b, state_path=state_file, enabled=True)
check('静音成功', a.mute() is True and b.muted is True)
saved = json.loads(state_file.read_text(encoding='utf-8'))
check('保存原状态', saved.get('muted') is True and saved.get('prev_mute') is False, saved)
check('重复静音不重复写入', a.mute() is True)
a.restore()
check('恢复原状态', b.muted is False)
check('状态已清除', json.loads(state_file.read_text(encoding='utf-8')).get('muted') is False)

# 模拟上次被强杀：状态文件里仍标记静音
state_file.write_text(json.dumps({'muted': True, 'prev_mute': True}), encoding='utf-8')
b2 = FakeBackend()
a2 = AudioMuter(backend=b2, state_path=state_file, enabled=True)
a2.recover()
check('启动时恢复遗留静音', b2.muted is True)
check('遗留状态已清除', json.loads(state_file.read_text(encoding='utf-8')).get('muted') is False)

# 状态文件被删（例如数据目录被清理）时，内存兜底仍能恢复
b3 = FakeBackend()
a3 = AudioMuter(backend=b3, state_path=state_file, enabled=True)
check('准备：再次静音', a3.mute() is True and b3.muted is True)
try:
    state_file.unlink()
except OSError:
    pass
a3.restore()
check('状态文件丢失也能恢复声音', b3.muted is False)


# ------------------------------------------------------------------ 数据库迁移

print('=== 数据库迁移 ===')
# 先造一个 2.1.x 的旧 settings 表
config.DATA_DIR.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(str(config.DB_PATH))
conn.execute('''CREATE TABLE settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    quota_minutes INTEGER NOT NULL DEFAULT 120,
    warn_minutes  TEXT    NOT NULL DEFAULT '10,5,1',
    admin_hash    TEXT    NOT NULL DEFAULT '',
    unlock_hash   TEXT    NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 0,
    locked        INTEGER NOT NULL DEFAULT 0,
    lock_reason   TEXT,
    lock_style    TEXT    NOT NULL DEFAULT 'bsod',
    bsod_code     TEXT    NOT NULL DEFAULT 'CRITICAL_PROCESS_DIED',
    updated_at    TEXT    NOT NULL DEFAULT ''
)''')
conn.execute("INSERT INTO settings (id, quota_minutes, updated_at) VALUES (1, 60, 'legacy')")
conn.commit()
conn.close()

from core import store  # noqa: E402

s = store.get_settings()
check('保留旧额度', int(s['quota_minutes']) == 60, s.get('quota_minutes'))
check('新列有默认热键', s.get('hotkey') == config.DEFAULT_HOTKEY, s.get('hotkey'))
check('新列默认显示托盘', int(s.get('tray_hidden') or 0) == 0, s.get('tray_hidden'))
check('新列默认禁用任务管理器', int(s.get('disable_taskmgr') or 0) == 1, s.get('disable_taskmgr'))
cols = {str(r['name']) for r in store.db().execute('PRAGMA table_info(settings)')}
check('hotkey 列已迁移', 'hotkey' in cols)
check('tray_hidden 列已迁移', 'tray_hidden' in cols)
check('disable_taskmgr 列已迁移', 'disable_taskmgr' in cols)

print('=== 指令分流 ===')
store.push_command('toast', {'text': 'hello'})
store.push_command('set_port', {'port': 18080})
only_toast = store.take_commands({'toast'})
check('只取 toast', [c['type'] for c in only_toast] == ['toast'], only_toast)
only_port = store.take_commands({'set_port'})
check('只取 set_port', [c['type'] for c in only_port] == ['set_port'], only_port)
check('没有剩余指令', store.take_commands() == [])

check('初始未完成设置', store.is_initialized() is False)
store.update_settings(admin_hash=store.hash_pwd('admin123'),
                      unlock_hash=store.hash_pwd('888888'))
check('两个密码齐全后视为已初始化', store.is_initialized() is True)
check('密码校验', store.verify_pwd('admin123', store.get_settings()['admin_hash']))
check('错误密码被拒绝', not store.verify_pwd('wrong', store.get_settings()['admin_hash']))

print('=== 登录限流 ===')
from web import server as web_server  # noqa: E402
web_server._login_fail.clear()
ip = '203.0.113.9'
check('初始不限流', web_server._login_blocked(ip) is False)
for _ in range(5):
    web_server._login_failed(ip)
check('5 次失败后限流', web_server._login_blocked(ip) is True)
web_server._login_ok(ip)
check('成功登录后解除限流', web_server._login_blocked(ip) is False)

if os.name == 'nt':
    print('=== 守护进程 ===')
    import subprocess  # noqa: E402
    import time  # noqa: E402

    import main as app_main  # noqa: E402

    keeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                              creationflags=0x08000000)
    try:
        # 停止标记：watchdog 看到就退出，且不再拉起程序
        config.WD_STOP.write_text('1', encoding='utf-8')
        started = time.monotonic()
        app_main.run_watchdog(keeper.pid, interval=0.05, max_rounds=20)
        elapsed = time.monotonic() - started
        check('看到停止标记后 watchdog 退出', elapsed < 2, f'{elapsed:.2f}s')
        check('没有拉起新实例', app_main.pid_alive(keeper.pid))

        # 停止：先落标记，再直接结束进程
        config.WD_PID.write_text(str(keeper.pid), encoding='utf-8')
        config.WD_STOP.unlink()
        app_main.kill_watchdog()
        check('停止后进程已退出', not app_main.pid_alive(keeper.pid))
        check('落下停止标记', config.WD_STOP.exists())
        check('确认死后清掉 pid 文件', not config.WD_PID.exists())
    finally:
        if keeper.poll() is None:
            keeper.kill()


print()
print('=== 全部通过 ===' if ok else '=== 存在失败项 ===')
sys.exit(0 if ok else 1)
