---
doc_type: plan
status: draft
---

# 桌面模式无控制台启动（消除多余 cmd/tail 窗口 + 根治 taskkill 误杀）

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-28 |
| **目标** | 桌面模式启动后无任何黑色控制台窗口，只有 pywebview 主窗口；根治关闭时 taskkill 误杀其他软件 cmd 窗口的问题 |
| **范围** | `run_desktop.bat`、`desktop.py` |
| **不做** | 不改 `run_dev.bat`（开发模式需要两个 cmd 窗口看 uvicorn/vite 输出）；不改 exe 打包逻辑（`console=False` 已无窗口） |

---

## 0. 背景与根因

### 0.1 现状

用户双击 `run_desktop.bat` 后：

1. 出现 1 个 cmd 窗口（bat 本身）——"Novel Analyzer Desktop Mode"
2. bat 用 `start "NovelAnalyzer 运行日志" powershell -NoExit ...` 开第 2 个窗口（tail 日志）
3. bat 同步启动 `desktop.py`，输出重定向到 `%LOCALAPPDATA%\NovelAnalyzer\run.log`
4. GUI 退出后 bat 执行 `taskkill /FI "WINDOWTITLE eq NovelAnalyzer 运行日志" /T /F` 关 tail 窗口
5. cmd 窗口自己 `pause`，等用户按键

共 2 个窗口（cmd + tail），关闭时 taskkill 产生误杀风险。

### 0.2 问题 1：tail 窗口的存在理由已经不成立

tail 窗口的原始动机是"日志重定向到 run.log 后，用户没有窗口可看"。
但现在 GUI 内已有实时日志控制台（WebSocket `log` 事件 + `LogConsole` 组件），
运行中的所有 uvicorn/业务日志都能看到。tail 窗口的信息完全冗余。

另外，run.log 会增长到几十 MB，用 `Get-Content -Wait` 持续读它是浪费；
用 `Get-Content -Tail 50 -Wait` 也只是显示尾部，超出 GUI 日志面板的功能。

### 0.3 问题 2：taskkill /FI WINDOWTITLE 有误杀风险

`taskkill /FI "WINDOWTITLE eq NovelAnalyzer 运行日志" /T /F` 依赖精确窗口标题匹配。
标题匹配在某些情况下不可靠：

- cmd `start "title"` 的 title 不总是稳定地保留在进程的窗口标题上
  （PowerShell -NoExit 不会改标题，但依赖这一点是脆弱的）
- 如果用户同时运行多个项目/脚本，其他进程的窗口标题也可能恰好包含
  "NovelAnalyzer 运行日志" 字样（不太可能，但只要存在就会误杀）
- `/T` 杀进程树：如果被匹配到的是 cmd 宿主而非 PowerShell 子进程，
  整棵树被强杀——包括"别的软件启动的 cmd 窗口"

既然 tail 窗口可以去掉，这条 taskkill 也一并消失，问题从根上解决。

### 0.4 问题 3：pythonw 下的 stdout/stderr 陷阱

如果直接把 `%PYTHON% desktop.py` 改成 `%PYTHON_W% desktop.py`
（`pythonw.exe`），以下代码会崩或产生怪异行为：

1. `desktop.py` 顶部 `sys.stdout.buffer` / `sys.stderr.buffer`：
   pythonw 下 `sys.stdout` / `sys.stderr` 是 `None`，不是 buffer 对象，
   `hasattr(None, "buffer")` 返回 False，所以当前 try/except 能安全跳过。
   但 `sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, ...)` 的 hasattr
   检查本身就依赖 stdout 不为 None——pythonw 下实际是 None，不会走到
   TextIOWrapper。所以这段代码在 pythonw 下"恰好"安全，但逻辑含糊。
   需要显式处理 None 情况。

2. `_install_crash_diagnostics` 中 `sys.stderr = _StderrTee(sys.stderr, crash_logger)`：
   pythonw 下 sys.stderr 是 None，_StderrTee 的 `self._orig.write(s)` 会
   AttributeError——但已有 `try/except Exception: pass` 包住，所以安全。
   只是 `self._orig` 是 None 而非文件对象，_StderrTee 的语义变得奇怪。

3. `_start_server` 的 uvicorn `log_config` 用 `"stream": "ext://sys.stderr"`：
   pythonw 下 sys.stderr 是 None，uvicorn 尝试构造 StreamHandler(None) 时
   可能抛错或行为未定义。`backend/main.py` 已经用 `log_config=None` 规避了
   这个问题，但 `desktop.py` 没有用 None。

4. `main()` 中 `print("小说智能分析器已在运行中")`：
   pythonw 下 print(None stream) 会抛异常，但它在 `except Exception:`
   分支内，异常会被吞掉，不影响主流程——只是这条提示永远不会显示。
   应改成 try/except 保护。

5. `logging.basicConfig(level=..., format=...)` 没有传 stream：
   Python logging 默认走 sys.stderr，pythonw 下是 None——
   StreamHandler(None) 的 emit 会静默失败，不 crash，但日志全部丢失。
   需要把 root logger 的输出指到 crash.log 或专用文件。

### 0.5 方向：pythonw + GUI 内日志

核心决策：

- 用 `pythonw.exe` 启动 `desktop.py`，彻底无控制台窗口
- 删除 tail 窗口和 taskkill，run.log 仍然完整落盘（供事后排查）
- uvicorn 的 log_config 改为 `None`（走已有 root logger 配置），
  而 root logger 的输出指向 `analyzer.log` / `crash.log`（已有 handler）
- `desktop.py` 顶部对 `sys.stdout is None` / `sys.stderr is None`
  显式处理：替换为一个安全的 `io.StringIO` 丢弃或直接保留 None，
  不依赖 hasattr 判断
- `print(...)` 那一处加 try/except 或改用 logger

这样：
- 双击 bat → 无窗口直接起 GUI
- GUI 内可看实时日志
- run.log / crash.log / analyzer.log 继续完整落盘
- 没有 taskkill，没有误杀风险

---

## 1. 设计决策

### D1：run_desktop.bat 改用 pythonw

bat 中定位 Python 的逻辑改为优先找 `pythonw.exe`：

1. `%SHARE_ROOT%\.venv\Scripts\pythonw.exe`（venv 优先）
2. `where pythonw`（PATH 中找）
3. `py -3 -c "import sys; print(sys.executable)"` 拿到 python 路径后，
   把文件名替换为 `pythonw.exe`（同目录一般都有）
4. 如果都找不到 pythonw，报错退出（和现在找不到 python 一样的处理）

另外，既然用 pythonw 启动，bat 自身的 cmd 窗口也不需要了——
bat 做完 robocopy/copy 后直接 `start "" "%PYTHON_W%" "%LOCAL_APP%\desktop.py"`
异步启动 GUI，然后 bat 立即 `exit /b 0`，cmd 窗口闪一下就关掉。

注意：`start ""` 的空标题是必须的（cmd 的 start 命令第一个引号参数被
解释为标题）。异步启动后 bat 的同步部分结束，`set RUN_ERR=%ERRORLEVEL%`
等逻辑不再需要。

### D2：删除 tail 窗口和 taskkill

`start "NovelAnalyzer 运行日志" powershell ...` 和
`taskkill /FI "WINDOWTITLE eq ..." /T /F` 两块整体删除。
相关注释一并清理。

### D3：desktop.py 顶部 stdout/stderr 显式处理

pythonw 下 `sys.stdout` / `sys.stderr` 是 None。实现采用"先补 None，再走
原有包装链"的顺序：

```python
if hasattr(sys.stdout, "buffer"):
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, ...)
```

1. `if sys.stdout is None: sys.stdout = open(os.devnull, "w", encoding="utf-8")`
   （stderr 同理）——保证后续 `_StderrTee._orig` 永不为 None，
   `__getattr__` 代理（isatty/fileno/encoding）也不会 AttributeError
2. 再走原有 `hasattr(..., "buffer")` → `TextIOWrapper` 逻辑（devnull 也有 buffer）
3. `_install_crash_diagnostics` 的 `_StderrTee` 不需要额外改动

```python
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, ...)
```

同理 stderr。用 `open(os.devnull, "w")` 保证后续任何 `.write()` 调用
不会 AttributeError，且底层是一个合法文件对象（devnull）。

### D4：uvicorn log_config 改为 None

`_start_server` 中的 log_config dict 删除，`uvicorn.run(..., log_config=None)`。
这样 uvicorn 不构造 StreamHandler(sys.stderr)——它走 root logger 配置。

而 root logger 在 `desktop.py` 顶部 `logging.basicConfig(level=INFO, format=...)`
时没有指定 stream，Python 默认会取 `sys.stderr`——pythonw 下是 None（或我们已
替换成 devnull）。StreamHandler(None) 或 StreamHandler(devnull) 都不会 crash，
只是输出丢弃——这正是我们要的：日志统一走 RotatingFileHandler 落盘。

但需要确认 root logger 上已挂的 RotatingFileHandler（analyzer.log 的 handler
在 `backend/app.py` 里挂）仍然工作——它不依赖 stderr，没问题。
crash.log 的 handler 也在 `_install_crash_diagnostics` 中独立挂上，同样没问题。

结论：uvicorn 的 stdout/stderr 输出在 pythonw 下会被丢弃（devnull），
但所有重要日志（uvicorn error、业务 logger、crash logger）都会通过
analyzer.log / crash.log 的 file handler 落盘。GUI 内的实时日志走 WebSocket，
不依赖 stdout。所以功能无损失。

**实现后必须实测**（评审补强项）：pythonw 启动应用，确认 analyzer.log
有 uvicorn / 后端启动日志落盘。若 `uvicorn.error` 未传播到 root logger
（不同 uvicorn 版本对 `log_config=None` 的 logger 配置不完全一致），在
`_start_server` 里显式设置 `logging.getLogger("uvicorn.error").propagate = True`
或追加 file handler 补齐。

### D5：print 调用加保护

`main()` 中"检测到已有实例在运行"分支的 `print(...)` 是给控制台用户看的。
pythonw 下 print 到 devnull 不报错（因为我们已把 stdout 换成 devnull），
所以其实不需要额外处理。但为了清晰，加个注释说明这条在无控制台下
不会显示（靠 MessageBox 已足够）。

### D6：去掉 bat 的 echo 输出改为写日志

bat 里的 `echo === Novel Analyzer Desktop Mode ===` 等用户可见输出——
由于 cmd 窗口会立即关闭，这些 echo 其实不会被看到。
保留无妨（在打开 bat 的终端里能看到，比如从 cmd 启动时），但
最重要的状态信息必须写进 run.log，当前已满足。

### D7：run.log 保持作为启动诊断

即使 GUI 内可看日志，run.log 仍是排查"双击没反应"类问题的唯一手段
（比如 robocopy 失败、pythonw 找不到等发生在 GUI 起来之前的错误）。
所以 bat 中的 `echo ... >> "%RUN_LOG%"` 全部保留。

### D8：失败路径的反馈

bat 在 pythonw 异步启动后立即退出，后续错误都发生在 desktop.py 里。
desktop.py 的错误路径（WebView2 缺失、依赖缺失、端口冲突等）都已经有
`MessageBoxW` 或 `logger.error` 覆盖。需要确认以下场景：

- `webview` ImportError → 目前 `sys.exit(1)` 无任何用户可见反馈
  → 加 `MessageBoxW` 提示（参照 WebView2 缺失的做法）
- `_wait_for_server` 超时 → 同样加 `MessageBoxW`
- 后端线程 `_start_server` 抛异常 → 目前 daemon 线程，异常会打日志
  → `_wait_for_server` 的超时检查能兜底，MessageBox 提示

### D9：run_dev.bat 不动

开发模式需要看到 uvicorn/vite 的实时输出，两个 cmd 窗口是合理的。

---

## 2. 任务清单

### Phase 1：desktop.py（预计 0.5 天）

| # | 文件 | 改动 | 验收 |
|---|---|---|---|
| T1.1 | `desktop.py` | 顶部 stdout/stderr None 显式处理（open devnull 或替换为安全 StringIO） | pythonw 启动不报错 |
| T1.2 | `desktop.py` | `_start_server` 删除 log_config dict，改 `log_config=None` | uvicorn 不崩，日志走 root logger |
| T1.3 | `desktop.py` | `main()` 中 webview ImportError 分支加 MessageBoxW | 用户能看到"pywebview 未安装"弹窗 |
| T1.4 | `desktop.py` | `_wait_for_server` 超时分支加 MessageBoxW | 用户能看到"后端启动超时"弹窗 |

### Phase 2：run_desktop.bat（预计 0.5 天）

| # | 文件 | 改动 | 验收 |
|---|---|---|---|
| T2.1 | `run_desktop.bat` | 定位 pythonw.exe（venv > PATH > py -3 替换） | 正确找到 pythonw 路径 |
| T2.2 | `run_desktop.bat` | 删除 tail 窗口 start + taskkill 两块及注释 | 无 PowerShell 窗口 |
| T2.3 | `run_desktop.bat` | `%PYTHON_W%` 用 `start "" ...` 异步启动 desktop.py；`exit /b 0` 直接结束 | cmd 窗口闪一下关掉 |
| T2.4 | `run_desktop.bat` | 移除 `set RUN_ERR` / 同步 `pause` / 同步 pushd-popd 尾部结构 | 无残留逻辑 |

### Phase 3：验证（预计 0.5 天）

| # | 项目 | 验收 |
|---|---|---|
| T3.1 | 手动双击 run_desktop.bat | GUI 弹出，无 cmd/tail 窗口残留 |
| T3.2 | 关闭 GUI | 无 taskkill，无 cmd 窗口残留，其他软件 cmd 不受影响 |
| T3.3 | 查看 `%LOCALAPPDATA%\NovelAnalyzer\run.log` | 启动步骤日志完整，无 UnicodeEncodeError |
| T3.4 | 查看 `analyzer.log` / `crash.log` | 后端日志正常，业务日志无丢失 |
| T3.5 | GUI 内日志面板 | WebSocket 日志正常 |
| T3.6 | `python -m pytest`（463 用例） | 全绿，无回归 |
| T3.7 | 新增单测：模拟 `sys.stdout=None`/`sys.stderr=None`，验证 desktop.py 顶部处理 + `_StderrTee` 不崩 | `backend/tests/test_desktop_pythonw.py` 通过 |
| T3.8 | 实测：pythonw 启动后确认 analyzer.log 有 uvicorn / 后端启动日志落盘 | uvicorn.error 传播路径生效 |

### Phase 4：文档（预计 0.2 天）

| # | 文件 | 改动 |
|---|---|---|
| T4.1 | `README.md` | 桌面模式描述：删除"tail 窗口"相关说明（如有） |
| T4.2 | `agent.md` | 记录本次改动的动机和方案 |

## 3. 风险与缓解

| 风险 | 缓解 |
|---|---|
| pythonw 下 print/TextIOWrapper 意外崩 | 顶部统一 None → devnull，`try/except` 兜底 |
| uvicorn StreamHandler(None) 意外崩 | log_config=None 走 root logger；root logger 走 file handler |
| 用户习惯看 tail 窗口 | GUI 内日志面板覆盖运行时需求；run.log 可事后打开 |
| 双击没反应（早期崩） | run.log 里有启动步骤日志；关键失败路径有 MessageBox |
| 老用户脚本依赖 bat 的同步行为（如等待退出码） | 场景极少；如需要可用 `python desktop.py` 手动启动 |

## 4. 不做（显式排除）

- 不改 `run_dev.bat`（开发模式需要两个 cmd 窗口）
- 不改 exe 打包（`console=False` 已无窗口，且 exe 用户不走 bat）
- 不改 WebSocket 日志机制（GUI 内实时日志不受本次影响）
- 不做 tray icon / 后台化（pywebview 无此需求，范围外）
