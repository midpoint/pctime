# 测试说明

全部测试是普通 Python 脚本，直接打印 `PASS/FAIL` 并以退出码 0/1 结束，不依赖 pytest。

## 一键运行

```powershell
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1
```

> ⚠️ 脚本会清空 `%LOCALAPPDATA%\PCTimeControlLite` 和 `HKCU\Software\PCTimeControlLite`，并真实启动客户端、触发锁屏。请在测试机或已备份数据的机器上运行。

## 测试顺序与依赖

| 脚本 | 依赖 | 覆盖 |
|---|---|---|
| `_test_web_lite.py` | 客户端已启动、8080 可用 | 首次设置、登录、本机设置、端口校验、启停、锁定/解锁、CSRF、路径穿越 |
| `_test_e2e_lite.py` | 上一步创建的管理密码 `admin123` / 解锁密码 `888888` | 时间耗尽直接蓝屏、手机解锁、暂停不锁 |
| `_test_units_lite.py` | Windows（仅守护进程段） | 快捷键、端口、连点、静音、数据库迁移、登录限流、指令分流、守护进程停止（真起子进程） |
| `_test_backdoor.py` | 无 | 连点 9 次不触发、第 10 次触发、超时不累计 |
| `_test_ui_lite.py` | 图形环境（Windows 桌面或 xvfb） | 进度、连点绑定、首次设置、后门密码框、控制面板/每日额度 |
| `_test_service_units.py` | 无 | 服务数据目录、备份恢复、JSON 重建、ACL 参数、服务命令、任务管理器策略 |

## 测试环境开关

`run_all.ps1` 会设置：

- `PCTL_DISABLE_AUDIO=1`：不真正静音
- `PCTL_SKIP_LOCAL_SETUP=1`：跳过本地首次设置窗口，改用网页 `/setup`
- `PCTL_SKIP_AUTOSTART=1`：不写开机自启

测试结束后会自动停止客户端、清理测试数据和测试密码，并把客户端日志保存到 `tests/last_run.log`（已被 `.gitignore` 忽略）。

## 单独运行

先启动客户端（建议设置上面的环境变量），再执行：

```bat
python tests\_test_web_lite.py
python tests\_test_e2e_lite.py
```

纯逻辑测试与 UI 测试不要求客户端：

```bat
python tests\_test_units_lite.py
python tests\_test_service_units.py
python tests\_test_backdoor.py
xvfb-run -a python tests\_test_ui_lite.py   # Linux 无桌面时
```

## 服务模式

服务模式的真实行为（SCM 安装、ACL 拒绝标准用户、SYSTEM 会话代理、任务管理器策略）无法由这些脚本自动覆盖，需要在 Windows 管理员 PowerShell 里手工验收：

```powershell
& ".\dist\PCTimeControlLite.exe" --install
# 验收后：
& "C:\Program Files\PCTimeControlLite\PCTimeControlLite.exe" --uninstall
```
