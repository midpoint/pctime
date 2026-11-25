# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Windows 单机版「儿童电脑使用时长控制」：Tkinter + Win32 ctypes 托盘程序（无主窗口）跑在孩子电脑上，内置 HTTP 服务让家长在同一 Wi-Fi 下用手机浏览器管理。本质不是 Web 应用，而是「手机网页写 SQLite → 主线程每秒轮询 → 计时/锁屏/解锁」的单机状态机。

- **仅支持 Windows**：`core/guard.py`、`ui/win32.py` 依赖 Win32 API；完整程序无法在 Linux/macOS 运行。`ui/win32.py` 和核心纯逻辑模块可以安全导入，便于跨平台单元测试。
- 两种运行模式：
  - **便携模式**：数据在 `%LOCALAPPDATA%\PCTimeControlLite\`，单进程 Tk + Web + 内置 watchdog。
  - **服务模式（2.3.0）**：程序在 `C:\Program Files\PCTimeControlLite\`，数据在 `C:\ProgramData\PCTimeControlLite\`，由 `PCTimeGuard`（SYSTEM 守护）、`PCTimeWeb`（LocalService 状态+Web）和用户会话中的 SYSTEM 代理组成。
- 自 2.2.0 起不再使用 PyQt5；运行时零第三方 Python 依赖（Tkinter 是标准库），打包只需要 PyInstaller。2.3.0 增加服务化强化保护；2.3.1 本地面板支持修改每日额度。
- 没有 lint / 格式化配置；测试是手写脚本，不是 pytest。

## 常用命令

在 Windows 上执行：

```bat
python main.py                 :: 便携模式（托盘图标，无窗口；需带 Tcl/Tk 的 Python 3.10+）
python main.py --port 8090     :: 临时改端口（会写回 config.json）
build.bat                      :: PyInstaller 单文件打包 -> dist\PCTimeControlLite.exe

:: 服务化强化（先打包，以下需要管理员）
PCTimeControlLite.exe --install
PCTimeControlLite.exe --uninstall
:: 内部模式：--service（PCTimeGuard）、--web（PCTimeWeb）、--agent --session N
```

- 安装/卸载代码只支持 frozen exe；源码模式直接 `--install` 会提示先打包。
- 服务模式的数据目录由 `--data-dir` 在**导入 core.config 之前**通过 `PCTL_DATA_DIR` 环境变量确定（见 `main._early_args`）。

开发/打包依赖：`python -m pip install -r requirements-dev.txt`（仅 PyInstaller/Ruff）；更完整的人类开发说明见 `CONTRIBUTING.md`，测试说明见 `tests/README.md`。

回归测试（会清空数据、启动真实客户端并真实触发锁屏）：

```powershell
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1
```

`run_all.ps1` 会删除数据目录和注册表用量，并设置 `PCTL_DISABLE_AUDIO=1`（不真正静音）、`PCTL_SKIP_LOCAL_SETUP=1`（跳过本地首次设置窗口，改用网页 `/setup`）、`PCTL_SKIP_AUTOSTART=1`（不写开机自启），然后按顺序运行六个脚本：

1. `_test_web_lite.py` —— 网页流程、本机设置、端口校验
2. `_test_e2e_lite.py` —— 时间耗尽锁屏、手机解锁
3. `_test_units_lite.py` —— 快捷键/端口/连点/静音状态机/数据库迁移/登录限流/指令分流（纯逻辑），以及守护进程停止（仅 Windows，真起并结束一个子进程）
4. `_test_backdoor.py` —— 连点后门纯逻辑
5. `_test_ui_lite.py` —— Tkinter 进度、连点绑定、首次设置、控制面板（需要图形环境）
6. `_test_service_units.py` —— 服务数据目录、备份恢复、ACL 参数、服务命令、任务管理器策略

服务模式的真实 Windows 行为（SCM、SYSTEM 会话代理、ACL、服务安装）无法在 Linux 验证；需要 Windows 手工验收。

- `_test_web_lite.py` 与 `_test_e2e_lite.py` 共享同一个运行中的客户端和同一份数据库；`_test_e2e_lite.py` 依赖前者的管理密码 `admin123`、解锁密码 `888888`，且都硬编码 `127.0.0.1:8080`，必须按此顺序跑。
- 单独跑某个测试：先设置上述环境变量并 `python main.py`，再执行对应脚本。
- `_test_units_lite.py` 会在导入 `core.config` 前把 `LOCALAPPDATA` 与 `PCTL_DATA_DIR` 都指向同一个临时目录（后者优先级更高）；`_test_backdoor.py` 不启动客户端、不显示窗口。
- 测试结束时 `run_all.ps1` 会停止客户端、保存日志到 `tests/last_run.log`，并删除测试数据/注册表，避免测试密码 `admin123` 残留。

手动重置到首次使用状态：删除 `%LOCALAPPDATA%\PCTimeControlLite` 和注册表 `HKCU\Software\PCTimeControlLite`。

## 架构

### 进程、线程与事件模型

`main.py` 的 `App` 同时持有：

| 组件 | 位置 | 职责 |
|---|---|---|
| Tk 主循环 | `main.py` `_tick_cb` | `root.after(1000)` 驱动 `tick()`；所有 Tk 操作只在这里 |
| `WebService` | `web/server.py` | 后台线程 `ThreadingHTTPServer`，监听 `0.0.0.0` |
| `TrayIcon` | `ui/win32.py` | 独立线程：隐藏消息窗口 + `Shell_NotifyIconW` + 右键菜单 |
| `HotkeyManager` | `ui/win32.py` | 独立线程：`RegisterHotKey` + `GetMessage` |
| `LockManager` | `ui/lock_screen.py` | 多屏全屏蓝屏/假弹窗、静音、冻结输入、本地密码后门 |
| `KeyboardGuard` | `core/guard.py` | 低级键盘钩子；锁屏时吞掉所有按键 |
| `ToastManager` | `ui/toast.py` | 家长留言横幅（Tk Toplevel） |
| `ControlPanel` / `SetupWindow` | `ui/panel.py` | 快捷键唤出的本地面板 / 首次运行强制设置 |
| watchdog | `main.py` | 独立子进程，主进程被杀后拉起 |

**硬性规则：Tk 只能在主线程调用。** Web 线程只读写 SQLite；托盘/热键线程只向 `queue.Queue` 投递事件 `('tray', id)`、`('tray_open',)`、`('hotkey',)`，主线程每 120 ms 通过 `_poll_events` 消费。

### 每秒状态机（`App.tick`）

1. `_sync_settings()`：只有 `settings.updated_at` 变化时才重新加载设置；同时应用 `hotkey`、`tray_hidden` 的变化。
2. 跨天：日期变了就重读当天用量。
3. 计时：`enabled=1` 且未锁屏时 `pending += 1`；每累计 5 秒落盘到 `usage`。
4. `_reconcile_used()`：手机端 reset/unlock 会直接清零数据库，这里检测到 DB 用量小于内存值时跟随。
5. 锁定判定：`should_lock = settings.locked OR (settings.enabled AND remaining <= 0)`。手动锁定无条件生效；**时间到只在「开始管控」状态下才锁**。
6. `handle_commands()`：消费 `commands` 表 —— `toast` 弹横幅；`lock`/`unlock` 写回设置；`set_port` 由主线程重启 HTTP 服务。
7. 更新托盘菜单/工具提示/控制面板。

状态语义（`settings.enabled` 与 `settings.locked` 相互独立）：

- `enabled`：是否正在计时。`POST /start`、面板「开始管控」置 1（同时清零用量、解除锁定）；`/pause` 置 0。
- `locked`：是否显示锁定画面。`/lock`、`App.do_lock()` 置 1；`/start`、`/pause`、`/unlock`、`/reset` 置 0。
- `POST /settings` **故意不碰 `enabled`**，避免改额度时把正在进行的管控关掉。

### 存储层（`core/store.py`）

- SQLite（WAL 模式）位于数据目录，表：`settings`（单行 id=1）、`usage`（按日期）、`commands`（一次性指令队列）、`events`（审计日志）。
- `settings` 在 2.2.0 新增 `hotkey TEXT DEFAULT 'Ctrl+Alt+M'`、`tray_hidden INTEGER DEFAULT 0`，2.3.0 新增 `disable_taskmgr INTEGER DEFAULT 1`；`db()` 启动时用 `PRAGMA table_info` + `ALTER TABLE` 自动迁移旧库。
- 用量双写：`set_used()` 同时写 SQLite 和注册表 `HKCU\Software\PCTimeControlLite\u_<日期>`（DWORD）；`get_used()` 取两者最大值。删掉 `data.db` 清不掉当天用量；`reset_used()` 会把 0 写进两边。
- `update_settings()` 只接受白名单字段，并总是刷新 `updated_at` —— 这是主线程感知配置变更的唯一信号。
- 密码：PBKDF2-SHA256、120,000 轮，格式 `salt_hex$dk_hex`；管理密码 ≥6 位，解锁密码 ≥4 位。

### Web 层（`web/`）

- `server.py` 路由：`GET /` `/settings` `/logs` `/login` `/setup` `/static/app.css`；`POST /setup /login /start /pause /settings /local /port /password /lock /unlock /reset /toast`。
- 未初始化（两个密码哈希任一为空）时，所有 GET 重定向到 `/setup`；`POST /setup` 仅在未初始化时可用。
- `/local` 修改 `hotkey`/`tray_hidden`（写 DB，主线程 1 秒内应用）；`/port` 校验 1024–65535、浏览器不安全端口、占用情况，然后向 `commands` 表推 `set_port`，由主线程执行 `WebService.stop()` + `start(strict_port=...)`；失败自动回滚旧端口。
- 会话：内存字典，Cookie `pctlite_sid`（HttpOnly、SameSite=Lax），TTL 12 小时；重启进程即全部失效。
- CSRF：POST 校验 `Origin`/`Referer` 的 netloc 等于 `Host`；两者都不带时放行（家用局域网场景）。
- `/login` 有内存限流：同一 IP 5 分钟内失败 5 次暂停 60 秒。
- `pages.py` 用 Python f-string 拼整页 HTML；任何动态内容必须经 `h()` 转义。样式在 `web/static/app.css`。
- 端口：启动时 `config.pick_port()` 避开不安全端口并探测占用；网页显式改端口时走严格模式，不会自动换端口。

### 首次运行

- `App.start()` 检查 `store.is_initialized()`；未设置密码时显示 `SetupWindow`，完成前不启动 Web/托盘/热键/计时。
- 设置窗口要求管理密码 ≥6、解锁密码 ≥4、额度 1–1440；关闭按钮无效；可退出程序，下次仍强制设置。
- 自动化测试用 `PCTL_SKIP_LOCAL_SETUP=1` 跳过本地窗口，改由网页 `/setup` 完成。

### 热键与托盘

- 默认 `Ctrl+Alt+M`；解析逻辑在 `core/hotkey.py`（纯逻辑，可在任意平台测试）。支持 Ctrl/Alt/Shift/Win + A–Z、0–9、F1–F12，且至少包含 Ctrl/Alt/Win 之一。
- `HotkeyManager` 用独立线程 `RegisterHotKey(..., MOD_NOREPEAT)`；注册失败时主线程回退旧值/默认值并写事件；若托盘被隐藏且没有可用热键，自动恢复托盘。
- 锁屏期间热键被忽略（`_poll_events` 检查 `locker.is_locked`）；非锁屏时 `show_panel()` 先校验管理密码，面板 3 分钟自动隐藏，避免孩子用默认热键暂停管控。
- `TrayIcon` 由独立线程持有隐藏消息窗口；菜单项是主线程下发的快照，菜单命令通过队列回传。图标从 `app.ico` 加载，失败退回系统默认图标。
- 托盘隐藏状态存 `settings.tray_hidden`；网页和面板都可切换；隐藏时启动用提醒横幅告知快捷键。

### 锁定与死机模拟（`ui/lock_screen.py`）

- `LockManager` 为每个显示器创建一个无边框置顶 Toplevel，每 900 ms 重新 `lift`/置顶；有密码框打开时跳过，避免压住对话框。
- `lock()`：创建蓝屏/假弹窗 → `AudioMuter.mute()`（Core Audio 静音默认播放设备）→ `KeyboardGuard.set_active(True)`（吞掉全部按键）→ `win32.keep_awake(True)` 阻止休眠/熄屏。
- `unlock()`：销毁窗口 → 取消 keep_awake → 关闭键盘钩子 → `AudioMuter.restore()`。
- 蓝屏进度 `BsodProgress` 每 900 ms 随机 +2~4，到 100% 后保持；所有窗口设置 `cursor="none"` 隐藏鼠标。
- 后门：`core.backdoor.ClickCounter` 在 6 秒内累计 10 次点击触发 `_PwdDialog`；显示密码框期间 `guard.set_typing(True)`：放行普通输入，仍吞掉 Win/Ctrl/Alt 组合键。
- `AudioMuter` 把静音状态写入数据目录 `audio.state`；进程被强杀后下次启动 `recover()` 恢复。测试用 `PCTL_DISABLE_AUDIO=1` 禁用。
- `Ctrl+Alt+Del` 无法拦截（Windows 安全序列），这是文档化的已知边界。

### 单实例与 watchdog

- `ui.win32.acquire_single_instance()` 用 `CreateMutexW('Local\\PCTimeLite_SingleInstance')`；句柄必须活到进程结束。
- `main.py --watchdog <pid>` 是 detached 子进程，每 3 秒检查目标 PID，退出后重启；连续 3 次新进程活不过 4 秒则熔断退出（每次失败后间隔 10 秒重试，不会一次失败就永久停摆）。自身 PID 写入 `watchdog.pid`。
- watchdog 与主进程各自拥有独立的 `_MEI` 解压目录：拉起子进程时设 `PYINSTALLER_RESET_ENVIRONMENT=1`。共用目录时，主进程退出删不掉解压目录（弹 “Failed to remove temporary directory”），被拉起的实例还会因运行库已随目录删除而弹 “Failed to start embedded python interpreter!”。
- 家长退出必须先 `kill_watchdog()` 再 `root.quit()/destroy()`。停法两道：先落停止标记 `watchdog.stop`（watchdog 看到就自行退出、绝不再拉起），再用 `TerminateProcess` 直接结束并等它退出。不要用 `taskkill` 子进程——在 `--noconsole` 构建里调用它会卡满超时而目标进程仍存活。
- 终止没成功时保留 `watchdog.pid`（留着下次启动才能找到它）；只有确认进程已死、或确认该 pid 不是本程序时才删除。`_is_our_exe()` 用来防 pid 复用误杀，认不出时只记日志不动手。
- 便携模式没有本地密码就无法打开面板（`show_panel` 校验管理密码，面板 3 分钟自动隐藏）；管理密码连续失败 5 次暂停 1 分钟。
- PyInstaller onefile 每次启动解压到 `%TEMP%\_MEI*`；`cleanup_stale_meipass()` 清理超过 24 小时的残留，watchdog 重启前额外等 1.2 秒。

### 服务化强化（2.3.0）

模式由 `main()` 分派：`--install/--uninstall` → `service.install`；`--service` → `service.guard`；`--web` → `service.web_core`；`--agent` → `service.agent`。

- 安装器在复制/迁移数据后会要求管理员现场完成首次设置（若数据库还没有密码），然后才创建服务；避免孩子抢先设置。
- **PCTimeGuardSvc / `service.guard`**（LocalSystem）：
  - `setup`：创建目录、应用 ACL（`service.acl`）、从备份恢复 DB（`service.backup`）、打开 `FileGuard` 防删除句柄；
  - 每 1s：枚举活动会话（`service.wts`），对非管理员会话持续写任务管理器策略、检查/启动 SYSTEM 代理；
  - 每 10s：备份 DB/设置到 `DataDir\backup` + HKLM（不含密码哈希）；每 30s 重应用 ACL；
  - 看护 `PCTimeWeb` 服务；停止时恢复任务管理器策略、停止 Web、结束代理。
- **PCTimeWebSvc / `service.web_core`**（LocalService）：状态机 + `WebService`，不计时/锁屏 UI；把 `locked` 写入 DB，代理轮询执行；`take_commands({'set_port','lock','unlock'})`，toast 留给代理。以 LocalService 运行，避免网络服务拿到 SYSTEM。
- **PCTimeAgent / `service.agent`**（SYSTEM，用户会话）：由守护服务用 `CreateProcessAsUser` + 复制 SYSTEM 令牌 + `TokenSessionId` 启动到 `winsta0\default`；负责托盘/热键/面板/锁屏/静音/键盘钩子/keep-awake；每 500ms 读 DB 同步 `locked`；`take_commands({'toast'})`；写 `runtime/agent_<session>.hb` 心跳。同一会话用 `Local\PCTimeAgent_<session>` 互斥。
- **安装器 `service.install`**：
  - 复制 exe 到 Program Files；迁移 `%LOCALAPPDATA%`/所有用户 profile 的便携数据到 ProgramData；
  - ACL：程序目录 SYSTEM/Admins 全控 + LocalService RX + Users RX（只读执行）；数据目录 SYSTEM/Admins/LocalService 全控，普通用户无权限；
  - 用 SCM API 创建两个服务（Web 依赖 Guard，失败自动重启）、防火墙规则、HKLM 安装信息；
  - 卸载校验管理/解锁密码（`--force` 可绕过），停删服务、结束代理、清任务管理器策略/ACL/防火墙/注册表/文件；运行中的已安装 exe 用 `MoveFileEx(DELAY_UNTIL_REBOOT)`。
  - 测试/文档：`CHANGELOG.md` 记录 2.2/2.3；`docs/images/` 是界面预览；`run_all.ps1` 结束会清理测试数据和密码。
- **任务管理器策略**：`settings.disable_taskmgr`（默认 1）；守护服务对非管理员会话写 `HKU\<SID>\...\Policies\System\DisableTaskMgr=1` 并每次轮询恢复；网页「本机设置」可关闭。
- **边界**：不实现真正的进程隐藏（需要内核驱动/rootkit）；标准用户不能结束 SYSTEM/LocalService 进程或删除受 ACL 保护的文件，但管理员、安全模式、物理接触仍可绕过。
- 服务模式的托盘由 SYSTEM（高完整性）代理创建，低完整性 Explorer 的托盘回调可能受 UIPI 限制；快捷键/本地面板/手机是可靠入口。

## 维护与发布

### 版本号同步

- `core/config.py` 的 `APP_VER`
- `version_info.txt`
- `pyproject.toml`，随后运行 `uv lock` 更新 `uv.lock`
- `CHANGELOG.md`

### 新增设置检查清单

1. `core/store.py`：`SCHEMA`、`_MIGRATIONS`、`get_settings` 默认值、`update_settings` 白名单。
2. `web/server.py` + `web/pages.py`：表单/路由/校验；POST 需登录与同源校验。
3. `ui/panel.py`：控件、`_sync_fields`；在 `main.App` 与 `service.agent.AgentApp` 实现方法并加入 `snapshot()`。
4. 生效逻辑：便携模式 `main.App.tick`/`_sync_settings`；服务模式 `service.web_core.CoreService.tick` 或 `service.agent.AgentApp._poll`。
5. 测试：纯逻辑加 `_test_units_lite.py`/`_test_service_units.py`；UI 加 `_test_ui_lite.py`；Web 加 `_test_web_lite.py`。
6. 文档：README、使用说明、CHANGELOG；架构变化更新本文件。

### 发布前检查

```bat
python -m compileall -q main.py autostart.py core ui web service tests
ruff check .
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1
build.bat
```

服务模式再手工验收：`--install`、标准用户进程/文件保护、任务管理器策略、备份恢复、`--uninstall`。

## 约定与陷阱

- 所有源文件头部保留作者署名（野怼怼 / https://www.bd7iwd.com）和 MIT 声明；注释、日志、界面文案统一用中文。
- 版本号需同步：`core/config.py` 的 `APP_VER`、`version_info.txt`、`pyproject.toml`（`uv.lock` 里的项目版本也一并改）。
- 不要用 6666（以及黑名单里的端口）作为服务端口：Chromium 系浏览器会直接报 `ERR_UNSAFE_PORT`。
- **不要从 Web/托盘/热键线程调用任何 Tk API**；一律投递到主线程队列。
- Tk 窗口在 `<Configure>` 事件里可能早于子类字段初始化，自绘窗口的 `_draw` 必须做 `hasattr` 守卫（见 `BsodScreen._draw`）。
- 副屏坐标为负时 Tk 的 `geometry("+x+y")` 会把它解释成「距右边缘」，必须用 `win32.place_topmost()` 的 `SetWindowPos` 路径。
- `.ps1` 文件必须保存为 UTF-8 with BOM，否则 PowerShell 5.1 解析中文会乱码/语法错误。
- 新增 `web/static` 资源时必须同步加到 `build.bat` 的 `--add-data`，否则打包后 404；服务模块用 `--collect-submodules service` 打进包。
- 服务模式的 `--data-dir` 必须在**导入 core.config 之前**处理（`main._early_args`），否则 `DATA_DIR` 会落到 LocalAppData。
- 服务模式的数据目录 ACL 故意不授予 Users；Web 服务是 LocalService、代理是 SYSTEM，二者都能访问，标准用户不能。不要把 Web 服务改成 SYSTEM。
- `FileGuard` 只持有 exe/数据目录/数据库的句柄，**不要**持有 `config.json`、`audio.state`（会被原子替换）和 SQLite 的 `-wal/-shm`（会被 SQLite 删除），否则会破坏写入。
- `WTSQueryUserToken`/`SetTokenInformation(TokenSessionId)`/`CreateProcessAsUser` 需要 SYSTEM 的 SeTcb/AssignPrimaryToken/IncreaseQuota 特权；`service.wts` 会显式启用。
- 不要尝试内核驱动/EPROCESS 断链来“隐藏进程”；本项目的合法边界是让标准用户无法结束/删除，而不是 rootkit。
- 打包时 `--noconsole` 会吞掉 stdout；诊断信息统一写 `client.log`（`core.config.log`）。
- `pyproject.toml` 是极简 uv 虚拟项目（`dependencies = []`），`src/` 脚手架已移除；真实入口是根目录 `main.py`，使用扁平导入（启动时 `sys.path.insert`）。
- 打包关键参数：`--onefile --noconsole --collect-submodules service --exclude-module PyQt5/PyQt6/PySide2/PySide6 --add-data "web\static;static" --add-data "app.ico;."`。
- 运行时静音只作用于 Windows 默认播放设备；测试环境务必保留 `PCTL_DISABLE_AUDIO=1`，避免把测试机静音。
