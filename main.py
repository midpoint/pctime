# -*- coding: utf-8 -*-
"""儿童电脑使用时长控制 · 单机版

一个 exe 全搞定：内置 Web 服务（手机访问 电脑IP:8080）+ 本地控制面板 +
托盘 + 计时 + 假蓝屏。界面使用 Tkinter + Win32 ctypes，不再依赖 PyQt5。

用法：
    python main.py                         便携模式
    python main.py --watchdog N            便携守护（内部使用）
    python main.py --port 8080             临时改端口
    python main.py --install / --uninstall 安装/卸载强化保护服务（管理员）
    python main.py --service / --web / --agent   服务模式（内部使用）

作者：野怼怼；https://www.bd7iwd.com
许可：MIT
"""
from __future__ import annotations

import glob
import os
import queue
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


def _early_args() -> None:
    """在导入 core.config 之前确定数据目录（服务模式用 ProgramData）。"""
    for i, a in enumerate(sys.argv):
        if a == '--data-dir' and i + 1 < len(sys.argv):
            os.environ['PCTL_DATA_DIR'] = sys.argv[i + 1]
        elif a.startswith('--data-dir='):
            os.environ['PCTL_DATA_DIR'] = a.split('=', 1)[1]
    if any(m in sys.argv for m in ('--install', '--uninstall',
                                   '--service', '--web', '--agent')):
        if not os.environ.get('PCTL_DATA_DIR'):
            base = os.environ.get('ProgramData') or r'C:\ProgramData'
            os.environ['PCTL_DATA_DIR'] = str(Path(base) / 'PCTimeControlLite')


_early_args()

import tkinter as tk
from tkinter import messagebox, simpledialog

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autostart
from core import audio, config, netinfo, store
from core import hotkey as hotkeylib
from core.guard import KeyboardGuard, pid_alive
from ui import win32
from ui.lock_screen import LockManager
from ui.panel import ControlPanel, SetupWindow
from ui.toast import ToastManager
from web.server import WebService

DETACHED = 0x00000008

# 托盘菜单项 ID
MENU_STATUS = 1
MENU_URL = 2
MENU_PANEL = 3
MENU_WEB = 4
MENU_AUTOSTART = 8
MENU_DATA = 9
MENU_EXIT = 10


# ------------------------------------------------------------------ 图标

def _bundled_icon_path() -> Path | None:
    """app.ico 的位置：打包后在 _MEIPASS，源码运行时在项目根目录。"""
    candidates = []
    mei = getattr(sys, '_MEIPASS', '')
    if mei:
        candidates.append(Path(mei) / 'app.ico')
    candidates.append(Path(__file__).resolve().parent / 'app.ico')
    for p in candidates:
        if p.exists():
            return p
    return None


def fmt_dur(sec: int) -> str:
    sec = max(0, int(sec))
    hh, mm, ss = sec // 3600, (sec % 3600) // 60, sec % 60
    if hh:
        return f'{hh}小时{mm}分'
    if mm:
        return f'{mm}分{ss}秒'
    return f'{ss}秒'


# ------------------------------------------------------------------ 主控制器

class App:
    def __init__(self, root: tk.Tk, events: queue.Queue):
        self.root = root
        self.events = events
        self.cfg = config.load_cfg()

        # 组件
        self.web = WebService()
        self.audio = audio.AudioMuter()
        try:
            self.audio.recover()
        except Exception as e:
            config.log(f'[audio] 启动恢复失败: {e}')
        self.guard = KeyboardGuard()
        self.guard.start()
        self.locker = LockManager(root, self.guard, self.audio,
                                  on_local_unlocked=self._on_local_unlocked)
        self.toasts = ToastManager(root)
        self.tray = win32.TrayIcon(events, icon_path=_bundled_icon_path())
        self.hotkey = win32.HotkeyManager(events)
        self.panel = ControlPanel(root, self)

        # 状态
        self.settings = store.get_settings()
        self._cfg_ver = self.settings.get('updated_at', '')
        self.quota = max(60, int(self.settings['quota_minutes']) * 60)
        self.used = store.get_used()
        self.pending = 0
        self.remaining = max(0, self.quota - self.used)
        self._cur_date = store.today()
        self._tick_n = 0
        self._written = self.used
        self._started = False
        self._active_hotkey = ''
        self._processed_hotkey = self.settings.get('hotkey') or config.DEFAULT_HOTKEY
        self._tray_hidden = int(self.settings.get('tray_hidden') or 0)
        self._setup_window: SetupWindow | None = None
        self._panel_prompting = False
        self._panel_fails: list[float] = []
        self._panel_block_until = 0.0

        self._poll_id = root.after(100, self._poll_events)

    # ------------------------------------------------------------ 启动

    def start(self) -> None:
        config.log(f'[app] 启动 v{config.APP_VER} data={config.DATA_DIR}')
        if not store.is_initialized():
            # 自动化测试可跳过本地设置窗口，改由网页 /setup 完成；
            # 正常运行时不设该变量，强制本地设置。
            if os.environ.get('PCTL_SKIP_LOCAL_SETUP', '') == '1':
                config.log('[setup] PCTL_SKIP_LOCAL_SETUP=1，跳过本地设置窗口')
            else:
                self._show_setup()
                return
        self._post_setup()

    def _show_setup(self) -> None:
        config.log('[setup] 未设置密码，弹出首次设置窗口')
        self._setup_window = SetupWindow(self.root, self._post_setup,
                                         self._exit_unconfigured)

    def _exit_unconfigured(self) -> None:
        config.log('[setup] 家长退出首次设置')
        self.shutdown(configured=False)
        kill_watchdog()          # 上一轮遗留的守护进程也不能留着，否则它会把程序拉起来
        try:
            self.root.destroy()
        except Exception:
            pass

    def _post_setup(self) -> None:
        if self._started:
            return
        self._started = True
        self._setup_window = None

        # 设置窗口可能刚写入密码/额度，重新读一遍
        self.settings = store.get_settings()
        self._cfg_ver = self.settings.get('updated_at', '')
        self.quota = max(60, int(self.settings['quota_minutes']) * 60)
        self.used = store.get_used()
        self._written = self.used
        self.remaining = max(0, self.quota - self.used)
        self._tray_hidden = int(self.settings.get('tray_hidden') or 0)

        ok = self.web.start()
        if not ok:
            config.log(f'[app] 网页服务启动失败，端口 {self.web.port} 可能被占用')

        tray_ok = self.tray.start(visible=not self._tray_hidden)
        if not tray_ok:
            config.log('[app] 托盘启动失败，快捷键与网页仍可使用')

        want_hk = self.settings.get('hotkey') or config.DEFAULT_HOTKEY
        hk_ok = self._register_hotkey(want_hk)
        if not hk_ok and want_hk != config.DEFAULT_HOTKEY:
            # 自定义热键不可用时退回默认值，避免完全没有本地入口
            hk_ok = self._register_hotkey(config.DEFAULT_HOTKEY)
            if hk_ok:
                store.update_settings(hotkey=config.DEFAULT_HOTKEY)
        if not hk_ok:
            config.log(f'[app] 快捷键 {want_hk} 注册失败')
            store.add_event('hotkey_failed', f'快捷键 {want_hk} 注册失败')
            if self._tray_hidden:
                # 没有托盘又没有热键会让家长无法在本地操作，强制恢复托盘
                self._tray_hidden = 0
                store.update_settings(tray_hidden=0)
                self.tray.set_visible(True)
            self.toasts.show(f'快捷键 {want_hk} 注册失败（可能被其他软件占用），'
                             '请在控制面板里换一个。', 12, 'warn')

        # 热键回退可能刚写过数据库，重新同步一次，避免 tick 反复重试
        self.settings = store.get_settings()
        self._cfg_ver = self.settings.get('updated_at', '')
        self._processed_hotkey = self.settings.get('hotkey') or config.DEFAULT_HOTKEY

        self._update_tray_ui()
        self.update_ui()

        self.tick()
        self._schedule_tick()

        url = netinfo.access_urls(self.web.port)[0]
        if self._active_hotkey:
            msg = f'手机访问 {url}\n快捷键 {self._active_hotkey} 打开控制面板'
        else:
            msg = f'手机访问 {url}\n全局快捷键不可用，请用托盘菜单或网页操作'
        if self.tray.visible:
            self.tray.notify('管控已启动', msg.replace('\n', '；'), 12000)
        else:
            self.toasts.show(f'管控已启动：{msg}', 10)

        store.add_event('started', f'程序启动 v{config.APP_VER}')

        try:
            if os.environ.get('PCTL_SKIP_AUTOSTART', '') != '1':
                # 每次启动都重写 Run 键，exe 被移动后路径也能自动更新
                autostart.enable()
            self._update_tray_ui()
        except Exception:
            pass

        if str(self.cfg.get('watchdog', 1)) == '1':
            spawn_watchdog()

    def _schedule_tick(self) -> None:
        try:
            self.root.after(1000, self._tick_cb)
        except Exception:
            pass

    def _tick_cb(self) -> None:
        self.tick()
        self._schedule_tick()

    def shutdown(self, configured: bool = True) -> None:
        try:
            if self.pending:
                store.add_used(self.pending)
                self.pending = 0
            if configured:
                store.add_event('stopped', '家长退出程序')
        except Exception:
            pass
        for fn in (self.audio.restore, self.web.stop):  # type: ignore[list-item]
            try:
                fn()
            except Exception:
                pass
        try:
            self.guard.set_active(False)
            self.guard.stop()
        except Exception:
            pass
        try:
            self.hotkey.stop()
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass

    # ------------------------------------------------------------ 每秒

    def tick(self) -> None:
        try:
            self._tick_n += 1
            self._sync_settings()
            self._reconcile_used()
            s = self.settings
            enabled = int(s['enabled']) == 1
            locked_flag = int(s['locked']) == 1

            # 跨天
            d = store.today()
            if d != self._cur_date:
                self._cur_date = d
                self.pending = 0
                self.used = store.get_used(d)
                self._written = self.used
                config.log(f'[app] 新的一天 {d}')

            # 计时
            if enabled and not self.locker.is_locked:
                self.pending += 1

            # 落盘（每 5 秒或进入锁定前）
            if self.pending >= 5:
                self.used = store.add_used(self.pending)
                self._written = self.used
                self.pending = 0

            self.remaining = max(0, self.quota - self.used - self.pending)

            # 状态机：手动锁定无条件生效；时间到只在计时中才锁
            should_lock = locked_flag or (enabled and self.remaining <= 0)
            if should_lock and not self.locker.is_locked:
                self.do_lock('manual' if locked_flag else 'quota')
            elif not should_lock and self.locker.is_locked:
                self.locker.unlock()
                self._after_unlock_ui()

            self.handle_commands()
            self.update_ui()
        except Exception as e:
            config.log(f'[tick] 异常: {e}')

    def _sync_settings(self) -> None:
        # 每秒都读一次：单行 SELECT 很便宜，能避免依赖时间戳时漏掉同一秒内的
        # 锁定/解锁切换（否则可能出现网页显示已解锁、电脑仍停在蓝屏）。
        s = store.get_settings()
        ver = s.get('updated_at', '')
        changed = ver != self._cfg_ver
        self._cfg_ver = ver
        self.settings = s
        self.quota = max(60, int(s['quota_minutes']) * 60)
        self.remaining = max(0, self.quota - self.used - self.pending)
        if changed:
            config.log(f'[cfg] 设置更新 quota={self.quota}s '
                       f'style={s.get("lock_style")} enabled={s["enabled"]}')

        want_hk = s.get('hotkey') or config.DEFAULT_HOTKEY
        if want_hk != self._processed_hotkey:
            self._processed_hotkey = want_hk
            if not self._register_hotkey(want_hk):
                old = self._active_hotkey or config.DEFAULT_HOTKEY
                config.log(f'[cfg] 快捷键 {want_hk} 注册失败，回退 {old}')
                store.add_event('hotkey_failed', f'快捷键 {want_hk} 注册失败，已回退')
                if old != want_hk:
                    store.update_settings(hotkey=old)
                self._processed_hotkey = old
                if self._tray_hidden:
                    self._tray_hidden = 0
                    store.update_settings(tray_hidden=0)
                    self.tray.set_visible(True)
                self.toasts.show(f'快捷键 {want_hk} 不可用（被占用或非法），'
                                 f'已保留 {old}。', 10, 'warn')
            else:
                self._processed_hotkey = self._active_hotkey or want_hk

        hidden = 1 if int(s.get('tray_hidden') or 0) else 0
        if hidden != self._tray_hidden:
            self._apply_tray_hidden(hidden)

    def _reconcile_used(self) -> None:
        """跟数据库对账：家长在手机上「重置/解锁」会把 used 清零，这里要认。"""
        db_used = store.get_used()
        if db_used + 2 < self._written:
            config.log(f'[sync] 检测到外部重置：{self._written}s → {db_used}s')
            self.used = db_used
            self.pending = 0
            self._written = db_used
        elif db_used > self._written:
            self.used = db_used
            self._written = db_used

    # ------------------------------------------------------------ 锁定

    def do_lock(self, reason: str) -> None:
        if self.locker.is_locked:
            return
        if self.pending:
            self.used = store.add_used(self.pending)
            self.pending = 0
        s = self.settings
        self.locker.lock(s.get('lock_style', 'bsod'),
                         s.get('bsod_code') or 'CRITICAL_PROCESS_DIED')
        try:
            store.update_settings(locked=1, lock_reason=reason)
        except Exception:
            pass
        store.add_event('locked', '时间用完，进入锁定画面'
                        if reason == 'quota' else '家长点了锁定')
        self.update_ui()

    def _on_local_unlocked(self) -> None:
        self.used = 0
        self.pending = 0
        self._written = 0
        self.remaining = self.quota
        config.log('[lock] 本地解锁，今日用时已清零')
        self.update_ui()

    def _after_unlock_ui(self) -> None:
        """远程解锁后同步内存用量（/unlock 会把数据库清零）。"""
        try:
            self.used = store.get_used()
            self.pending = 0
            self._written = self.used
            self.remaining = max(0, self.quota - self.used)
        except Exception:
            pass
        self.update_ui()

    # ------------------------------------------------------------ 指令

    def handle_commands(self) -> None:
        for cmd in store.take_commands():
            t = cmd.get('type')
            pl = cmd.get('payload') or {}
            if t == 'toast':
                text = str(pl.get('text') or '').strip()
                if text:
                    config.log(f'[cmd] 手机提醒: {text}')
                    self.toasts.show(text, int(pl.get('seconds') or 10),
                                     'warn' if pl.get('level') == 'warn' else 'info')
                    try:
                        self.tray.notify('📢 家长提醒', text, 8000)
                    except Exception:
                        pass
            elif t == 'lock':
                store.update_settings(locked=1, lock_reason='manual')
            elif t == 'unlock':
                store.update_settings(locked=0, lock_reason=None)
                store.reset_used()
            elif t == 'set_port':
                self._handle_set_port(pl.get('port'))

    def _handle_set_port(self, port) -> None:
        ok, msg = self.apply_port(port)
        config.log(f'[port] {msg}')
        if not ok:
            try:
                self.tray.notify('端口修改失败', msg, 10000, warning=True)
            except Exception:
                pass

    # ------------------------------------------------------------ 热键/托盘

    def _register_hotkey(self, text: str) -> bool:
        ok, canon = hotkeylib.normalize_hotkey(text)
        if not ok:
            return False
        old = self._active_hotkey
        if self.hotkey.start(canon):
            self._active_hotkey = canon
            config.log(f'[hotkey] 已注册 {canon}')
            return True
        # 注册失败：尽量恢复旧热键；旧值为空时尝试默认值
        if old and old != canon:
            self.hotkey.start(old)
        elif not old:
            fallback = config.DEFAULT_HOTKEY
            if fallback != canon and self.hotkey.start(fallback):
                self._active_hotkey = fallback
        return False

    def save_hotkey(self, text: str) -> tuple[bool, str]:
        ok, canon = hotkeylib.normalize_hotkey(text)
        if not ok:
            return False, canon
        if not self._register_hotkey(canon):
            return False, f'快捷键 {canon} 注册失败（可能被其他软件占用）。'
        store.update_settings(hotkey=canon)
        self._processed_hotkey = canon
        store.add_event('hotkey_changed', f'快捷键改为 {canon}')
        return True, canon

    def _apply_tray_hidden(self, hidden: int) -> None:
        self._tray_hidden = int(hidden)
        if not self._started:
            return
        if hidden:
            if not self._active_hotkey:
                # 没有热键又隐藏托盘会让本地无法操作，禁止
                self._tray_hidden = 0
                store.update_settings(tray_hidden=0)
                self.toasts.show('请先设置可用的全局快捷键，再隐藏托盘图标。', 8, 'warn')
                self.tray.set_visible(True)
                return
            if not self.tray.set_visible(False):
                self._tray_hidden = 0
                store.update_settings(tray_hidden=0)
                self.toasts.show('托盘图标隐藏失败，已保持显示。', 8, 'warn')
                return
            self.toasts.show(f'托盘图标已隐藏，按 {self._active_hotkey} 可唤出控制面板。', 8)
        else:
            self.tray.set_visible(True)
        self._update_tray_ui()

    def set_tray_hidden(self, hidden: bool) -> tuple[bool, str]:
        hidden = 1 if hidden else 0
        if hidden and not self._active_hotkey:
            return False, '请先设置一个可用的全局快捷键，否则隐藏托盘后将无法在本地打开控制面板。'
        if hidden:
            if not self.tray.set_visible(False):
                return False, '托盘图标隐藏失败（托盘可能未启动）。'
        else:
            self.tray.set_visible(True)
        self._tray_hidden = hidden
        store.update_settings(tray_hidden=hidden)
        store.add_event('tray_hidden' if hidden else 'tray_shown',
                        '隐藏托盘图标' if hidden else '显示托盘图标')
        return True, ''

    def _menu_items(self) -> list[dict]:
        st = self._status_text()
        urls = netinfo.access_urls(self.web.port)
        locked = self.locker.is_locked
        items = [
            {'id': MENU_STATUS, 'label': st, 'enabled': False},
            {'id': MENU_URL, 'label': f'手机访问：{urls[0]}', 'enabled': False},
            {'sep': True},
            {'id': MENU_WEB, 'label': '打开手机管理页', 'enabled': True},
        ]
        if not locked:
            items += [
                {'id': MENU_PANEL, 'label': '打开控制面板（需密码）', 'enabled': True},
            ]
        items += [
            {'sep': True},
            {'id': MENU_AUTOSTART, 'label': '开机自动启动',
             'enabled': not locked, 'checked': autostart.is_enabled()},
            {'id': MENU_DATA, 'label': '打开数据目录', 'enabled': True},
            {'sep': True},
            {'id': MENU_EXIT, 'label': '退出（需要解锁密码）', 'enabled': True},
        ]
        return items

    def show_panel(self) -> None:
        """本地控制面板需要管理/解锁密码，避免孩子用默认快捷键暂停管控。"""
        if self.locker.is_locked or self._panel_prompting:
            return
        self._panel_prompting = True
        try:
            now = time.time()
            if now < self._panel_block_until:
                messagebox.showwarning('密码尝试过多', '请稍后再试。', parent=None)
                return
            s = store.get_settings()
            if s.get('admin_hash'):
                pwd = simpledialog.askstring('家长验证', '请输入管理密码：',
                                             show='*', parent=None)
                if pwd is None:
                    return
                if not store.verify_pwd(pwd, s['admin_hash']):
                    self._panel_fails = [x for x in self._panel_fails
                                         if now - x <= 300] + [now]
                    if len(self._panel_fails) >= 5:
                        self._panel_fails.clear()
                        self._panel_block_until = now + 60
                        messagebox.showerror('密码错误', '尝试次数过多，请 1 分钟后再试。',
                                             parent=None)
                    else:
                        messagebox.showerror('密码错误', '管理密码不正确。', parent=None)
                    return
                self._panel_fails.clear()
            self.panel.show()
        finally:
            self._panel_prompting = False

    def _handle_tray_command(self, cmd: int) -> None:
        if cmd == MENU_PANEL:
            self.show_panel()
        elif cmd == MENU_WEB:
            self.open_web()
        elif cmd == MENU_AUTOSTART:
            if autostart.is_enabled():
                autostart.disable()
            else:
                autostart.enable()
            self._update_tray_ui()
        elif cmd == MENU_DATA:
            try:
                os.startfile(str(config.DATA_DIR))
            except Exception as e:
                config.log(f'[app] 打开数据目录失败: {e}')
        elif cmd == MENU_EXIT:
            self.request_exit(None)

    def _poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                try:
                    if self.root.grab_current() is not None:
                        continue      # 已有模态框（密码框/消息框），避免叠加
                except Exception:
                    pass
                if kind == 'hotkey':
                    if self.locker.is_locked:
                        config.log('[hotkey] 锁屏期间忽略唤出请求')
                    else:
                        self.show_panel()
                elif kind == 'tray_open':
                    self.show_panel()
                elif kind == 'tray':
                    self._handle_tray_command(int(ev[1]))
        except queue.Empty:
            pass
        except Exception as e:
            config.log(f'[events] 异常: {e}')
        try:
            self._poll_id = self.root.after(120, self._poll_events)
        except Exception:
            pass

    # ------------------------------------------------------------ 面板动作

    def snapshot(self) -> dict:
        port = self.web.port or int(self.cfg.get('port', config.DEFAULT_PORT))
        urls = netinfo.access_urls(port)
        return {
            'port': port,
            'url': urls[0] if urls else '',
            'hotkey': self._active_hotkey or self.settings.get('hotkey') or config.DEFAULT_HOTKEY,
            'tray_hidden': int(self._tray_hidden),
            'enabled': int(self.settings.get('enabled') or 0) == 1,
            'locked': self.locker.is_locked,
            'remaining': self.remaining,
            'quota': self.quota,
            'quota_minutes': max(1, int(self.settings.get('quota_minutes') or 120)),
            'used': self.used,
        }

    def save_quota(self, minutes) -> tuple[bool, str]:
        try:
            q = int(str(minutes).strip())
        except (TypeError, ValueError):
            return False, '请输入 1–1440 之间的整数分钟。'
        if not (1 <= q <= 1440):
            return False, '每日额度需在 1–1440 分钟之间。'
        store.update_settings(quota_minutes=q)
        store.add_event('quota_changed', f'每日额度改为 {q} 分钟')
        self.settings = store.get_settings()
        self._cfg_ver = self.settings.get('updated_at', '')
        self.quota = max(60, q * 60)
        self.remaining = max(0, self.quota - self.used - self.pending)
        self.update_ui()
        return True, str(q)

    def panel_start(self) -> None:
        if self.locker.is_locked:
            return
        store.reset_used()
        store.update_settings(enabled=1, locked=0, lock_reason=None)
        store.add_event('parent_start', '本地面板点了「开始管控」')
        self.used = 0
        self.pending = 0
        self._written = 0
        self.remaining = self.quota
        self.update_ui()

    def panel_pause(self) -> None:
        store.update_settings(enabled=0, locked=0, lock_reason=None)
        store.add_event('parent_pause', '本地面板点了「暂停管控」')
        self.update_ui()

    def panel_lock(self) -> None:
        if self.locker.is_locked:
            return
        store.update_settings(locked=1, lock_reason='manual')
        store.add_event('parent_lock', '本地面板点了「立即锁定」')
        self.do_lock('manual')

    def panel_unlock(self) -> None:
        store.reset_used()
        store.update_settings(locked=0, lock_reason=None)
        store.bump_unlock()
        store.add_event('unlocked', '本地面板解除锁定，今日用时已清零')
        self._after_unlock_ui()

    def open_web(self) -> None:
        try:
            webbrowser.open(f'http://127.0.0.1:{self.web.port}/')
        except Exception as e:
            config.log(f'[app] 打开管理页失败: {e}')

    def apply_port(self, port) -> tuple[bool, str]:
        try:
            p = int(port)
        except (TypeError, ValueError):
            return False, '端口必须是数字。'
        current = int(self.web.port or config.DEFAULT_PORT)
        if p == current and self.web.httpd is not None:
            return True, f'端口未变化（{p}）。'
        ok, msg = config.validate_port(p)
        if not ok:
            return False, msg
        old = current
        self.web.stop()
        if self.web.start(strict_port=p):
            store.add_event('port_changed', f'端口已切换到 {p}')
            self.update_ui()
            return True, f'已切换到 http://127.0.0.1:{p}/ （手机请用电脑 IP 访问）'
        # 回滚
        self.web.start(strict_port=old)
        store.add_event('port_change_failed', f'端口 {p} 启动失败，已恢复 {old}')
        return False, f'端口 {p} 启动失败，已恢复 {old}。'

    def request_exit(self, parent) -> bool:
        s = store.get_settings()
        if s.get('unlock_hash'):
            pwd = simpledialog.askstring('退出管控', '请输入解锁密码：',
                                         show='*', parent=parent)
            if pwd is None:
                return False
            if not store.verify_pwd(pwd, s['unlock_hash']):
                messagebox.showerror('密码错误', '密码不正确，无法退出。', parent=parent)
                return False
        config.log('[app] 家长退出')
        self.shutdown()
        kill_watchdog()          # 先干掉守护，否则它会立刻把我们拉起来
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        return True

    # ------------------------------------------------------------ UI 刷新

    def _status_text(self) -> str:
        if self.locker.is_locked:
            return '状态：已锁定（蓝屏中）'
        if int(self.settings['enabled']) != 1:
            return '状态：待机（未开始计时）'
        return f'状态：计时中 · 剩余 {fmt_dur(self.remaining)}'

    def _update_tray_ui(self) -> None:
        try:
            self.tray.set_menu(self._menu_items())
        except Exception:
            pass

    def update_ui(self) -> None:
        try:
            st = self._status_text()
            self.tray.set_tooltip(f'{config.APP_TITLE} — {st}')
            self._update_tray_ui()
        except Exception:
            pass
        try:
            if self.panel.winfo_exists() and self.panel.winfo_viewable():
                self.panel.refresh()
        except Exception:
            pass


# ------------------------------------------------------------------ Watchdog

def watchdog_running() -> bool:
    try:
        p = int(config.WD_PID.read_text(encoding='utf-8').strip())
        return pid_alive(p) and p != os.getpid()
    except Exception:
        return False


def _is_our_exe(pid: int) -> bool:
    """pid 对应的进程是不是同一个 exe（防止 pid 复用后误杀无关进程）。"""
    try:
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, 0, int(pid))       # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(1024)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return False
        finally:
            k32.CloseHandle(h)
        # 源码运行时守护进程用的是 pythonw.exe，与本进程的 python.exe 同目录，一并认下
        image = Path(buf.value).resolve()
        here = Path(sys.executable).resolve()
        return image in {here, here.with_name('python.exe').resolve(),
                         here.with_name('pythonw.exe').resolve()}
    except Exception:
        return False


def terminate_pid(pid: int, wait_ms: int = 3000) -> bool:
    """直接结束进程并等它退出，返回是否确认已死。

    不用 taskkill：它是外部控制台程序，在 --noconsole 的本程序里调用曾两次卡满
    5 秒超时、目标进程却仍存活——守护进程没停掉，退出后它就把程序重新拉起来。
    """
    try:
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x0001 | 0x00100000, 0, int(pid))   # TERMINATE | SYNCHRONIZE
        if not h:
            return not pid_alive(pid)
        try:
            k32.TerminateProcess(h, 1)
            k32.WaitForSingleObject(h, int(wait_ms))
        finally:
            k32.CloseHandle(h)
    except Exception as e:
        config.log(f'[watchdog] 终止 pid={pid} 出错: {e}')
    return not pid_alive(pid)


def kill_watchdog() -> None:
    """停掉守护进程。家长主动退出时必须先做，否则它会把程序又拉起来。

    终止不一定成功（权限、杀软、进程卡住），所以先落一个停止标记：
    守护进程看到标记就自行退出，绝不会再把程序拉起来。
    """
    try:
        pid = int(config.WD_PID.read_text(encoding='utf-8').strip())
    except Exception:
        pid = 0
    alive = bool(pid) and pid != os.getpid() and pid_alive(pid)
    ours = alive and _is_our_exe(pid)
    if alive:
        # 不管认不认得这个 pid，先落停止标记：它看到就不会再把程序拉起来
        try:
            config.WD_STOP.write_text(str(int(time.time())), encoding='utf-8')
        except Exception:
            pass
    if ours:
        if terminate_pid(pid, 3000):
            config.log(f'[watchdog] 已停止守护进程 pid={pid}')
        else:
            config.log(f'[watchdog] 未能终止守护进程 pid={pid}，已留停止标记')
    elif alive:
        config.log(f'[watchdog] pid={pid} 不是本程序进程，已忽略并清理 pid 文件')
    # 只有确认是自己、且还活着才保留 pid 文件；其余都是陈旧记录，删掉才能重新拉起
    if not (ours and pid_alive(pid)):
        try:
            config.WD_PID.unlink()
        except Exception:
            pass


def cleanup_stale_meipass() -> None:
    """清理 TEMP 下超过 24 小时的 _MEI 解压残留。"""
    if not getattr(sys, 'frozen', False):
        return
    import tempfile
    tmp = tempfile.gettempdir()
    cur = os.path.abspath(getattr(sys, '_MEIPASS', '') or '')
    now = time.time()
    removed = 0
    for p in glob.glob(os.path.join(tmp, '_MEI*')):
        if not os.path.isdir(p):
            continue
        if cur and os.path.abspath(p) == cur:
            continue
        try:
            if now - os.path.getmtime(p) < 86400:
                continue
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        except Exception:
            pass
    if removed:
        config.log(f'[app] 清理了 {removed} 个过期的 _MEI 解压残留目录')


def spawn_watchdog() -> None:
    kill_watchdog()          # 先了结上一轮遗留的守护进程，免得它盯着旧 pid 乱拉起
    if watchdog_running():
        # 终止没成功：停止标记会让它自行退出。等它退干净再起，不能就这么裸奔
        for _ in range(20):
            time.sleep(0.25)
            if not watchdog_running():
                break
    if watchdog_running():
        config.log('[watchdog] 旧守护进程未退出，本会话没有守护（已留停止标记）')
        return
    try:
        config.WD_STOP.unlink()      # 清掉上一轮留下的停止标记
    except Exception:
        pass
    base = config.app_dir()
    # 让守护进程有自己的 _MEI 解压目录：与主进程共用时，主进程退出清理解压目录会被它锁住，
    # 于是弹出 “Failed to remove temporary directory”。
    env = dict(os.environ)
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    if getattr(sys, 'frozen', False):
        args = [sys.executable, '--watchdog', str(os.getpid())]
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name('pythonw.exe')
        args = [str(pyw if pyw.exists() else exe), str(base / 'main.py'),
                '--watchdog', str(os.getpid())]
    try:
        subprocess.Popen(args, cwd=str(base), creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env)
        config.log('[watchdog] 已启动')
    except Exception as e:
        config.log(f'[watchdog] 启动失败: {e}')


def run_watchdog(pid: int, interval: float = 3.0, max_rounds: int | None = None) -> None:
    """守护主进程：每 interval 秒看一眼目标，退出就重新拉起。

    interval / max_rounds 只是测试用的覆盖点，默认值与线上行为一致。
    """
    base = config.app_dir()
    try:
        config.WD_PID.write_text(str(os.getpid()), encoding='utf-8')
    except Exception:
        pass
    config.log(f'[watchdog] 守护 target={pid}')
    # 拉起的新实例要自带 _MEI 解压目录：继承本进程的目录时，主进程退出会把这目录删掉，
    # 新实例随即因加载不到 python 运行库弹 “Failed to start embedded python interpreter!”。
    env = dict(os.environ)
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    if getattr(sys, 'frozen', False):
        args = [sys.executable]
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name('pythonw.exe')
        args = [str(pyw if pyw.exists() else exe), str(base / 'main.py')]

    target = pid
    fails = 0
    rounds = 0
    while True:
        time.sleep(interval)
        rounds += 1
        if config.WD_STOP.exists():
            config.log('[watchdog] 收到停止标记，退出')
            break
        if max_rounds is not None and rounds >= max_rounds:
            config.log('[watchdog] 到达运行轮次上限，退出')
            break
        if target and pid_alive(target):
            continue
        # 熔断：连续 3 次拉起后都活不过 4 秒，说明有别的实例占着或环境异常，
        # 继续拉只会变成死循环，直接放弃。
        if fails >= 3:
            config.log('[watchdog] 连续拉起失败，守护进程退出')
            break
        # 给旧进程留一点时间彻底释放文件句柄，否则新实例解压可能撞车
        time.sleep(1.2)
        config.log('[watchdog] 主进程已退出，重新拉起')
        try:
            p = subprocess.Popen(args, cwd=str(base), creationflags=DETACHED,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=env)
        except Exception as e:
            config.log(f'[watchdog] 拉起失败: {e}')
            fails += 1
            target = 0
            time.sleep(10)
            continue

        target = p.pid
        time.sleep(4)
        if pid_alive(target):
            fails = 0
        else:
            fails += 1
            config.log(f'[watchdog] 新进程未能存活（第 {fails} 次）')
            target = 0
            time.sleep(10)


# ------------------------------------------------------------------ 入口

def _service_installed() -> bool:
    if os.name != 'nt':
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'Software\PCTimeControlLite', 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, 'Installed')
            return int(val) == 1
    except Exception:
        return False


def _installed_notice() -> int:
    port = config.DEFAULT_PORT
    try:
        import json
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'Software\PCTimeControlLite', 0, winreg.KEY_READ) as k:
            port = int(winreg.QueryValueEx(k, 'Port')[0])
            data_dir = Path(str(winreg.QueryValueEx(k, 'DataDir')[0]))
        try:
            cfg = json.loads((data_dir / 'config.json').read_text(encoding='utf-8'))
            port = int(cfg.get('port') or port)
        except Exception:
            pass
    except Exception:
        pass
    url = netinfo.access_urls(port)[0]
    text = (f'强化保护服务正在运行。\n\n手机访问：{url}\n\n'
            '孩子登录标准用户后会自动启动 SYSTEM 代理；管理员账户不受锁屏影响。\n'
            '卸载请以管理员身份运行安装目录下的 exe：--uninstall')
    try:
        ctypes = __import__('ctypes')
        ctypes.windll.user32.MessageBoxW(None, text, config.APP_TITLE, 0x40)
    except Exception:
        config.log('[app] ' + text.replace('\n', ' '))
    return 0


def main() -> int:
    if '--install' in sys.argv:
        from service.install import install
        return install()
    if '--uninstall' in sys.argv:
        from service.install import uninstall
        return uninstall()
    if '--service' in sys.argv:
        from service.guard import run_guard
        return run_guard()
    if '--web' in sys.argv:
        from service.web_core import run_web_core
        return run_web_core()
    if '--agent' in sys.argv:
        from service.agent import run_agent
        return run_agent()

    if '--watchdog' in sys.argv:
        try:
            i = sys.argv.index('--watchdog')
            pid = int(sys.argv[i + 1])
        except Exception:
            pid = 0
        run_watchdog(pid)
        return 0

    # 已安装时双击 Program Files 里的 exe，只提示服务正在运行
    try:
        from service import paths as svc_paths
        if (_service_installed()
                and Path(sys.executable).resolve() == svc_paths.installed_exe().resolve()):
            return _installed_notice()
    except Exception:
        pass

    # 允许命令行改端口
    if '--port' in sys.argv:
        try:
            i = sys.argv.index('--port')
            cfg = config.load_cfg()
            cfg['port'] = int(sys.argv[i + 1])
            config.save_cfg(cfg)
        except Exception:
            pass

    win32.set_dpi_awareness()

    if not win32.acquire_single_instance():
        config.log('[app] 已有实例在运行')
        return 0

    # 清理残留放到单实例之后：已有一个实例（及其拥有独立 _MEI 的 watchdog）在跑时，
    # 它解压目录的 mtime 可能早就过了 24 小时，此时扫掉会把活用的文件删一半。
    cleanup_stale_meipass()

    root = tk.Tk()
    root.withdraw()
    root.title(config.APP_TITLE)

    def _tk_error(exc, val, tb):
        config.log(f'[tk] {getattr(exc, "__name__", exc)}: {val}')

    root.report_callback_exception = _tk_error

    events: queue.Queue = queue.Queue()
    ctrl = App(root, events)
    root.after(0, ctrl.start)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
