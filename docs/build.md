# 打包文档（PyInstaller）

本文档说明如何把 Novel Analyzer 打成单文件 Windows exe。

## 产出

- `dist/novel-analyzer.exe` — 约 **32 MB** 单文件（Python runtime + 依赖 + 前端 dist + 配置模板）

## 前置条件

1. Python 3.11+ venv（项目自带 `.venv/`）
2. 装好打包工具：
   ```bash
   uv pip install --python .venv\Scripts\python.exe pyinstaller pyinstaller-hooks-contrib
   ```
3. 前端已构建（`frontend/dist/` 存在）。如果不存在，先 `npm --prefix frontend run build`

## 一键打包

```cmd
desktop\build_exe.bat
```

或手动（在仓库根目录执行）：

```bash
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean desktop\desktop.spec
```

## 文件角色

| 文件 | 角色 |
|---|---|
| `desktop/desktop.py` | 桌面端入口（本地运行时副本兼容、单实例锁、原生窗口与崩溃诊断） |
| `desktop/desktop.spec` | PyInstaller 配置（hiddenimports + excludes + datas） |
| `desktop/config.example.json` | 嵌入 exe 的配置模板（首次启动复制为运行目录下的 `config.json`） |
| `desktop/_welcome_workspace.md` | 嵌入 exe 的 workspace 欢迎页（首次启动复制到 `workspace/README.md`） |
| `frontend/dist/` | 前端构建产物（PyInstaller 嵌入） |
| `build/` | PyInstaller 中间产物（不入库） |
| `dist/` | PyInstaller 产物（不入库，单独走 `gh release`） |

## 关键设计

### 数据落 EXE 同级（不走 `%APPDATA%`）

设计原则：打包便携版的所有用户数据（`workspace/` `config.json` `analyzer.log` `queue_state.json` `session.token` `app.lock`）都落在 **EXE 同级目录**（源码运行模式下则统一落在 `<repo>/runtime/`），避免：

- C 盘 `%APPDATA%` 残留
- 卸载残留
- 整目录迁移不便

实现：`backend/config/paths.py` 提供单一路径解析真源，区分 `CODE_ROOT`（代码包）、`BUNDLE_DIR`（只读嵌入资源）与 `RUNTIME_DIR`（可写用户数据：`frozen` 时为 EXE 同级，源码态默认为 `<repo>/runtime/`），由 `backend/app.py`、`backend/services/queue_manager.py` 与 `desktop/desktop.py` 共用。

### 自动收集 backend 子模块

`desktop/desktop.spec` 在顶层把仓库根目录加入 `sys.path` 后调用 `collect_submodules('backend')` 自动发现整个 backend 包的所有子模块（除 tests）。**解决 uvicorn.run("backend.app:app") 字符串 import 被 PyInstaller 静态分析漏掉的问题**。

### 首次启动行为

`first_run_setup()`：
1. 创建 `workspace/` 目录 + 复制欢迎页
2. 复制 `config.example.json` → `config.json`
3. 返回 `"NEEDS_API_KEY"` 触发引导到设置页（`window.evaluate_js("location.hash = '#/settings'")`）

### WebView2 Runtime 检测

`check_webview2()`：注册表 Edge Update Client + 路径检测双保险。
Win11 预装；Win10 部分用户首次启动需下载 ~100MB。

## 常见问题

### 1. PermissionError: 'dist\novel-analyzer.exe' 拒绝访问

**原因**：旧 exe 进程还在运行。

**解决**：
```powershell
Get-Process -Name novel-analyzer | Stop-Process -Force
# 或手动关掉弹出的 pywebview 窗口
```

### 2. ModuleNotFoundError: No module named 'xxx'

**原因**：PyInstaller 静态分析漏掉了动态 import。

**解决**：在 `desktop.spec` 的 `hiddenimports` 列表里加这个模块名。**优先**用 `collect_submodules('your_package')`。

### 3. exe 启动后秒退

**原因**：通常是 hiddenimports 不全，或 PyInstaller exclude 了运行时需要的模块。

**排查**：
1. 看 `dist/crash.log`
2. 在 `desktop/desktop.py` main() 加 try/except，把异常打出来
3. 命令行跑 exe 看错误（`dist\novel-analyzer.exe` 不要用 `Start-Process`）

### 4. 杀软误报

**原因**：PyInstaller 通用问题，无代码签名证书。

**缓解**：
- 不上 UPX（已设置 `upx=False`）
- README 注明"如被杀软拦截请添加信任"
- 长期方案：申请代码签名证书（约 $200-400/年）

### 5. Win10 用户首次启动提示"未检测到 WebView2"

**解决**：到 https://developer.microsoft.com/microsoft-edge/webview2/ 下载「Evergreen Standalone Installer」安装一次（~100MB）。

## 体积与构建取舍

| 取舍 | 影响 |
|---|---|
| excludes 大量未用 stdlib / 三方库（tkinter / matplotlib / numpy / pandas / scipy / PIL / IPython / pytest / black / flake8 / mypy） | 减小约 50 MB |
| 不上 UPX | 体积不变，换来更低的杀软误报率 |
| `collect_submodules` 自动收集 | 避免漏包导致重打，收益是修 bug 性质 |

## 未来优化方向

- **应用图标**：`desktop/desktop.spec` 设 `icon='assets/icon.ico'`（需要做 ico 文件）
- **代码签名**：申请证书 + spec 加 `codesign_identity`（减少杀软误报）
- **Nuitka 替代 PyInstaller**：编译为 C，更快更小（首次配置复杂）
- **macOS / Linux**：pywebview 跨平台需要单独打包（暂不做）
