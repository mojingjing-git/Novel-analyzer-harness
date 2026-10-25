"""
pywebview 桌面入口

启动流程：
1. 找一个可用端口
2. 后台线程启动 uvicorn (FastAPI)
3. 轮询 /api/health 等待后端就绪
4. pywebview 打开原生窗口 (1600x900)
5. 窗口关闭时终止全部

注意：处理 Windows 中文路径下相对导入失败的问题：
- 强制切换 cwd 到项目根目录（脚本所在目录）
- 把项目根目录加入 sys.path
- 任何路径相关操作都用绝对路径
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# === 关键：Windows 下 stdout/stderr 默认按 GBK 编码；本项目大量 print 含中文/emoji
# （如 ⚠️），一旦重定向到文件（run_desktop.bat 的 run.log）就会抛 UnicodeEncodeError，
# 进而让配置加载、请求处理等链路整体崩溃、所有接口返回 500。
# 重配为 UTF-8 + errors='replace'，确保任何 print 都不会中断进程。
import io as _io
try:
    # pythonw 下 sys.stdout/stderr 是 None。先补成合法文件对象（devnull），
    # 后续 TextIOWrapper / _StderrTee / __getattr__ 代理才不会拿到 None。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

# === 关键：解决中文路径下 import backend.app 失败 ===
# NOVEL_ROOT 允许从“本地副本”启动进程（WebView2 要求进程路径在本地盘），
# 同时把所有数据/日志仍指向共享盘上的真实项目根目录。
# PyInstaller 打包后（v0.2.0 exe）：所有用户数据落在 EXE 同级目录，不在
# %APPDATA% 也不在 _MEIPASS——避免 C 盘残留、卸载残留、便于整目录迁移。
if getattr(sys, "frozen", False):
    EXE_DIR = Path(sys.executable).resolve().parent   # exe 同级（可写，用户数据）
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR))  # 临时解压（只读，dist 在这）
else:
    EXE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = EXE_DIR
PROJECT_ROOT = Path(os.environ.get("NOVEL_ROOT") or str(EXE_DIR)).resolve()
os.chdir(str(PROJECT_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("desktop")


def _install_crash_diagnostics():
    """
    把未捕获异常 / 线程异常 / 原生 stderr 输出全部落盘，便于排查「闪退」。
    默认情况下桌面端 stderr 没有文件落盘，进程被 os._exit 或渲染进程崩溃带走时
    任何 traceback / pywebview(WebView2) 原生报错都会丢失，难以定位。

    H15 修复（2026-08-26）：原实现中 _StderrTee 用独立 open("a") 句柄直接写 crash.log，
    这与 crash_logger 的 RotatingFileHandler 持有同一文件的另一句柄——Windows 下
    os.rename(crash.log → crash.log.1) 会因 ERROR_SHARING_VIOLATION 失败，
    异常被 logging.handleError 捕获后又写回 stderr（即 crash.log），形成「永远不轮转 +
    永远增长」的反馈环，实测 crash.log 单文件涨到 60MB 无 .1 备份。

    修法：让 stderr 写入也走 crash_logger 的 logging 体系（统一 stream、统一锁、
    自动跟随 RotatingFileHandler.doRollover）；不再持有独立 open() 句柄，Windows
    上不再有共享锁竞争。
    """
    import sys
    import threading

    crash_path = PROJECT_ROOT / "crash.log"
    crash_logger = logging.getLogger("crash")
    crash_logger.setLevel(logging.ERROR)
    crash_logger.propagate = False
    if not any(
        getattr(h, "baseFilename", "") == str(crash_path)
        for h in crash_logger.handlers
    ):
        # 与 analyzer.log 同策略（10MB × 3），保证单文件不超过 10MB。
        fh = RotatingFileHandler(
            str(crash_path), encoding="utf-8",
            maxBytes=10 * 1024 * 1024, backupCount=3,
        )
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        crash_logger.addHandler(fh)

    def log_exc(msg, exc_info):
        crash_logger.error(msg, exc_info=exc_info)
        # 同时保留解释器默认行为（打印到原 stderr）
        sys.__excepthook__(*exc_info) if exc_info else None

    def _excepthook(t, v, tb):
        log_exc("Uncaught exception (main thread)", (t, v, tb))

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        log_exc(
            "Uncaught exception in thread",
            (args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook

    # 重定向 stderr 到「原 stderr + crash.log（走 logging）」，捕获 pywebview / WebView2 原生报错。
    # H15 关键：不再独立 open()，让 stderr 写入借 crash_logger 的 RotatingFileHandler
    # （同一流、同一锁、轮转自动跟随），消除 Windows ERROR_SHARING_VIOLATION。
    class _StderrTee:
        """std.stderr 替换实现：把原生 stderr 写入既送到原 stderr（控制台），
        也通过 crash_logger.error 走统一的 logging 体系（自动轮转）。"""
        _FLUSH_INTERVAL = 0  # 行缓冲依赖 logging 内部行为；此处不重复 flush

        def __init__(self, original, logger):
            self._orig = original
            self._logger = logger

        def write(self, s):
            # 1) 先送到原 stderr（控制台输出、第三方工具 attachConsole 等）
            try:
                self._orig.write(s)
            except Exception:
                pass
            # 2) 持久化：去掉末尾空白（logging 会再加换行），避免重复空行
            if s and s.strip():
                try:
                    self._logger.error("%s", s.rstrip())
                except Exception:
                    # logger 自身抛异常时静默吞掉，不影响 stderr 主通道
                    pass

        def flush(self):
            try:
                self._orig.flush()
            except Exception:
                pass
            # logging 体系内部已管理 flush，无需手动触发
            try:
                for h in self._logger.handlers:
                    h.flush()
            except Exception:
                pass

        def __getattr__(self, name):
            # 任何未实现的方法/属性都代理给原 stderr（如 .isatty、.fileno、.encoding 等）
            return getattr(self._orig, name)

    sys.stderr = _StderrTee(sys.stderr, crash_logger)
    logger.info(f"崩溃诊断已启用，原生报错将写入: {crash_path}")


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check_webview2() -> str | None:
    """检测 WebView2 Runtime（Win10 部分用户首次启动需下载 ~100MB）

    双保险：注册表 Edge Update Client + 路径检测
    返回版本字符串，未装返回 None
    """
    if sys.platform != "win32":
        return None  # macOS/Linux 走 pywebview 自带 webkit/gtk
    # 方法 1: 注册表 Edge Update Client ID
    try:
        import winreg
        for sub_key in (
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_key)
                version, _ = winreg.QueryValueEx(key, "pv")
                winreg.CloseKey(key)
                if version:
                    return version
            except FileNotFoundError:
                continue
    except Exception:
        pass
    # 方法 2: 路径检测
    for path in (
        r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
        r"C:\Program Files\Microsoft\EdgeWebView\Application",
    ):
        if Path(path).exists():
            return "detected-by-path"
    return None


def first_run_setup() -> str:
    """首次启动：自动创建 workspace + 复制 config.example.json 到 config.json

    Returns:
        "OK" - 已就绪
        "NEEDS_API_KEY" - 已生成 config.json，待用户填 API Key（启动后引导到设置页）
    """
    workspace = PROJECT_ROOT / "workspace"
    config = PROJECT_ROOT / "config.json"

    # 1. workspace 目录
    if not workspace.exists():
        try:
            workspace.mkdir(parents=True)
        except PermissionError as e:
            logger.error(f"无法创建 workspace 目录 {workspace}: {e}（EXE 所在目录不可写？）")
            raise
        # 复制欢迎页（PyInstaller 模式下 BUNDLE_DIR=_MEIPASS；开发模式 = EXE_DIR）
        try:
            src = BUNDLE_DIR / "_welcome_workspace.md"
            if src.exists():
                shutil.copy(str(src), str(workspace / "README.md"))
                logger.info(f"首次启动：已创建 workspace 并写入欢迎页")
            else:
                logger.warning(f"首次启动：欢迎页模板不存在 {src}（开发模式正常）")
        except Exception as e:
            logger.warning(f"复制欢迎页失败: {e}")

    # 2. config.json
    if not config.exists():
        try:
            src = BUNDLE_DIR / "config.example.json"
            if src.exists():
                shutil.copy(str(src), str(config))
                logger.info(f"首次启动：已生成 config.json，模板来自 config.example.json")
                return "NEEDS_API_KEY"
            else:
                logger.warning(f"首次启动：config.example.json 不存在 {src}（开发模式正常）")
        except PermissionError as e:
            logger.error(f"无法写入 config.json {config}: {e}（EXE 所在目录不可写？）")
            raise

    return "OK"


LOCK_FILE = PROJECT_ROOT / "app.lock"
SESSION_TOKEN_FILE = PROJECT_ROOT / "session.token"
SESSION_TOKEN_ENV = "NOVEL_ANALYZER_SESSION_TOKEN"


def _read_lock() -> dict:
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _instance_alive(lock: dict) -> bool:
    """以锁中端口做健康检查判定活实例（比 PID 存活检测跨平台可靠）"""
    port = lock.get("port")
    if not port:
        return False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
        return True
    except Exception:
        return False


def _tasks_running(port: int) -> bool:
    """分析或总结任一在运行即返回 True；查询失败（后端已挂）视为未运行，放行关闭"""
    for url in (f"http://127.0.0.1:{port}/api/analysis/status",
                f"http://127.0.0.1:{port}/api/summary/status"):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("running"):
                return True
        except Exception:
            continue
    return False


def _confirm_exit(window) -> bool:
    """弹原生确认框；返回 True=允许退出"""
    return bool(window.create_confirmation_dialog(
        "确认退出",
        "分析或总结任务正在进行中。\n已完成的进度已落盘，下次启动可断点续跑。\n确定要退出吗？"))


def _start_server(port: int):
    import uvicorn
    # pythonw / PyInstaller 无控制台时，uvicorn 默认 log_config 会访问
    # sys.stdout.isatty() 导致崩溃；log_config=None 让 uvicorn 走 root logger，
    # 输出由 backend/app.py 挂的 analyzer.log RotatingFileHandler 落盘。
    # 仅把 uvicorn.access 的高频 GET 请求日志压到 WARNING（对齐旧 log_config），
    # uvicorn.error 保留 INFO 并 propagate 到 root logger 落盘。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    uvicorn.run(
        "backend.app:app",
        host="127.0.0.1",
        port=port,
        log_config=None,
        app_dir=str(PROJECT_ROOT),
    )


def _wait_for_server(port: int, timeout: int = 30) -> bool:
    for _ in range(timeout * 2):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


class Api:
    """暴露给前端的 pywebview JS API（window.pywebview.api）"""

    def __init__(self, port: int):
        self._port = port

    def pick_files(self):
        """
        打开系统文件对话框选择 txt / docx 小说，返回文件路径列表。
        （Windows WebView2 的 <input type=file> 不给 File 对象注入 path 属性，
        必须走后端 create_file_dialog 才能拿到真实本地路径。）
        """
        import webview

        window = webview.windows[0]
        # file_types 必须是「并列字符串」，写成嵌套元组会失效
        # （pywebview 要求每个元素都是 str）。
        return window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=("TXT 文件 (*.txt)", "Word 文档 (*.docx)"),
        )

    def minimize(self):
        """最小化窗口（标题栏按钮）"""
        import webview
        try:
            webview.windows[0].minimize()
        except Exception:
            pass

    def toggle_maximize(self):
        """最大化 / 还原窗口（标题栏按钮）

        pywebview 的 win.maximized 是构造参数（始终等于初始值 False，不反映
        当前状态），win.restore() 注释也说"Restore minimized window"——用于
        unmaximize 不可靠。直接用 ctypes 调 Win32 IsZoomed + ShowWindow：
        - IsZoomed(hwnd) 真实查询当前窗口状态（最大化/正常）
        - ShowWindow(SW_MAXIMIZE=3 / SW_RESTORE=9) 切到目标状态
        两个 API 都是线程安全的，无需 marshal 到 UI 线程（Form.WindowState
        setter 才有这个限制，Win32 窗口级 API 不受）。
        """
        import ctypes
        hwnd = _get_hwnd()
        if hwnd is None:
            return
        user32 = ctypes.windll.user32
        SW_MAXIMIZE = 3
        SW_RESTORE = 9
        if user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_MAXIMIZE)

    def is_maximized(self) -> bool:
        """查询窗口是否处于最大化状态（前端 resize 时调以同步 UI 状态）

        前端用 innerWidth vs screen.width 启发式判定不可靠（用户拖窗口到
        接近屏宽时会误判为最大化），需要后端用 Win32 IsZoomed 真实查询。
        """
        import ctypes
        hwnd = _get_hwnd()
        if hwnd is None:
            return False
        try:
            return bool(ctypes.windll.user32.IsZoomed(hwnd))
        except Exception:
            return False

    def close(self):
        """关闭窗口（标题栏按钮）：任务运行中先弹确认"""
        import webview
        try:
            win = webview.windows[0]
            if _tasks_running(self._port) and not _confirm_exit(win):
                return
            win.destroy()
        except Exception:
            pass

    def move(self, dx, dy):
        """按增量移动窗口（标题栏拖拽）"""
        import webview
        try:
            win = webview.windows[0]
            win.move(int(win.x + dx), int(win.y + dy))
        except Exception:
            pass

    def get_session_token(self) -> str:
        """返回会话 token，前端 fetch 据此附加 X-Session-Token header（PR-1 收尾）。

        信任域前提：pywebview js_api 只暴露给 webview 当前加载的页面，而本应用
        webview 只加载本地 dist（不渲染远程内容），与 /ws/progress 的 Origin
        白名单同一信任模型——单用户本地应用中 webview JS 与 token 同信任域，
        不构成提权。未设环境变量（浏览器 dev 模式）返回空串，前端保持 null、
        不附加 header（后端 env 未设时本就放行）。token 不落日志、不进 localStorage。
        """
        return os.environ.get(SESSION_TOKEN_ENV, "")


def _get_hwnd() -> int | None:
    """从 pywebview Window 拿到 Win32 窗口句柄（hwnd），不可用时返回 None

    pywebview 的 Window 对象要等 webview.start() 之后 .native 才会被赋值；
    frameless 模式下 .native 仍是 WinForms Form，其 .Handle 属性就是
    Control.Handle 包装的 IntPtr。getattr 容错是为了处理 webview.start()
    之前的早期调用（如 pywebviewready 事件后立刻触发的情况）。
    """
    import webview
    try:
        win = webview.windows[0]
    except (IndexError, AttributeError):
        return None
    native = getattr(win, "native", None)
    if native is None:
        return None
    handle = getattr(native, "Handle", None)
    if handle is None:
        return None
    try:
        hwnd = int(handle.ToInt64() if hasattr(handle, "ToInt64") else handle)
    except Exception:
        return None
    return hwnd if hwnd != 0 else None


def main():
    _install_crash_diagnostics()

    # === v0.2.0 exe 首次启动逻辑 ===
    # 1. 检测 WebView2 Runtime（Win10 部分用户首次启动需下载 ~100MB）
    wv2 = check_webview2()
    if wv2 is None:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "未检测到 WebView2 Runtime。\n\n"
                "请到：\nhttps://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                "下载并安装「Evergreen Standalone Installer」（约 100MB）。\n"
                "安装一次后即可正常使用本应用。",
                "WebView2 缺失",
                0x40
            )
        except Exception:
            logger.error("WebView2 未安装，请到 https://developer.microsoft.com/microsoft-edge/webview2/ 下载")
        sys.exit(1)
    logger.info(f"WebView2 已就绪: {wv2}")

    # 2. 首次启动 setup：自动创建 workspace + 复制 config.json
    first_run_status = first_run_setup()
    if first_run_status == "NEEDS_API_KEY":
        logger.info("首次启动：需要填 API Key，启动后引导到设置页")

    port = _find_free_port()
    logger.info(f"启动后端服务: http://127.0.0.1:{port}")
    logger.info(f"项目根目录: {PROJECT_ROOT}")

    # 单实例保护（2026-08-17 优化）：
    # 原实现在服务器就绪后才写锁，竞态窗口长达数秒——两个实例同时启动、在“都还没写锁”的
    # 窗口内会双双通过 exists() 检查并各自起服务。
    # 现改为：抢到端口后立即用 os.open(O_CREAT|O_EXCL) 原子创建锁文件，窗口缩到微秒级；
    # 抢到锁即写入端口，让随后启动的实例立刻读到锁并做健康检查：活实例→提示退出，
    # 端口未就绪/进程已崩→视为上次残留，删锁后重试抢锁（最多 3 次）。
    for _attempt in range(3):
        try:
            _lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            old_lock = _read_lock()
            if _instance_alive(old_lock):
                logger.warning("检测到已有实例在运行，本实例退出")
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0, "小说智能分析器已在运行中。\n如确认无实例运行，请删除项目根目录的 app.lock 后重试。",
                        "已在运行", 0x40)
                except Exception:
                    print("小说智能分析器已在运行中")
                sys.exit(0)
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
    else:
        logger.error("无法获取单实例锁（反复抢锁失败），退出")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"无法获取单实例锁（反复抢锁失败）。\n\n请检查 {LOCK_FILE} 是否可删除。",
                "启动失败",
                0x10
            )
        except Exception:
            pass
        sys.exit(1)
    try:
        os.write(_lock_fd, json.dumps(
            {"pid": os.getpid(), "port": port, "started_at": time.time()},
            ensure_ascii=False).encode("utf-8"))
        os.fsync(_lock_fd)
    except OSError as e:
        logger.warning(f"写入单实例锁失败（不影响启动）: {e}")
    finally:
        try:
            os.close(_lock_fd)
        except OSError:
            pass

    # 生成 session token（PR-1 修复，D 方案鉴权，2026-09-10）
    # desktop.py 启动时生成 32 字节 url-safe token：
    #   1) 写到 session.token（工作目录 app.lock 旁）
    #   2) 设置 NOVEL_ANALYZER_SESSION_TOKEN 环境变量
    #   3) 后端 uvicorn（在同一 Python 解释器线程中跑）读环境变量启用 Depends 鉴权
    # 退出时通过 _on_closed 清理（删 session.token + 清环境变量）
    # Refs: _plan_v3.md PR-1 步骤 4
    try:
        _session_token = secrets.token_urlsafe(32)
        SESSION_TOKEN_FILE.write_text(_session_token, encoding="utf-8")
        os.environ[SESSION_TOKEN_ENV] = _session_token
        logger.info(f"已生成 session token 写入 {SESSION_TOKEN_FILE.name}")
    except Exception as e:
        logger.error(f"生成 session token 失败: {e}")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"生成 session token 失败，桌面模式无法安全启动。\n\n{e}\n\n"
                f"请检查 {PROJECT_ROOT} 是否可写。",
                "启动失败",
                0x10
            )
        except Exception:
            pass
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
        sys.exit(1)

    # 后台线程启动后端
    t = threading.Thread(target=_start_server, args=(port,), daemon=True)
    t.start()

    # 等后端就绪
    if not _wait_for_server(port):
        logger.error("后端启动超时")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "后端服务启动超时。\n\n请查看日志文件：\n"
                f"{Path(os.environ.get('LOCALAPPDATA', '')) / 'NovelAnalyzer' / 'run.log'}\n"
                f"{PROJECT_ROOT / 'analyzer.log'}",
                "启动失败",
                0x10
            )
        except Exception:
            pass
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
        sys.exit(1)
    logger.info("后端已就绪")

    # 打开原生窗口
    try:
        import webview
    except ImportError:
        _msg = "pywebview 未安装，桌面模式无法启动。\n\n请运行:\npip install pywebview\n\n详细日志见 analyzer.log / run.log"
        logger.error(_msg.replace("\n\n", " ").replace("\n", " "))
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, _msg, "依赖缺失", 0x10)
        except Exception:
            pass
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
        try:
            SESSION_TOKEN_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        sys.exit(1)

    window = webview.create_window(
        "小说智能分析器",
        f"http://127.0.0.1:{port}/?v={int(time.time())}",
        width=1600,
        height=900,
        min_size=(960, 700),
        text_select=True,
        js_api=Api(port),
        frameless=True,  # 隐藏系统标题栏，由前端 48px 一体化标题栏接管（VSCode/Discord 同款）
        transparent=True,  # Win11 Mica：WebView2 底色透明，让 DWM 背景透出（见 _apply_win11_backdrop）
    )

    # 记录窗口关闭 / 前端加载异常，便于区分「用户主动关」还是「渲染进程崩溃」
    def _on_closed():
        logger.warning("窗口已关闭（可能是用户主动关闭，也可能是渲染进程崩溃）")
        # 清理 session token（PR-1 修复，D 方案鉴权）
        # 删文件 + 清环境变量，避免下次启动时残留 token 被滥用
        try:
            os.environ.pop(SESSION_TOKEN_ENV, None)
        except Exception:
            pass
        try:
            SESSION_TOKEN_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _on_closing():
        """OS 关窗（Alt+F4/任务栏关闭）拦截：任务运行中需确认，返回 False 否决关闭"""
        try:
            if _tasks_running(port):
                if not _confirm_exit(window):
                    return False
        except Exception as e:
            logger.error(f"关闭确认检查异常，放行关闭: {e}")
        return True

    def _on_load_exc(e):
        logger.error(f"前端页面加载异常: {e}")

    try:
        window.events.closing += _on_closing
        window.events.closed += _on_closed
        window.events.load_exception += _on_load_exc
    except Exception as e:
        logger.debug(f"注册窗口事件回调失败（pywebview 版本差异）: {e}")

    def _apply_win11_backdrop():
        """Win11 宿主质感：DWM Mica 背景 + 深浅色适配。

        透明 WebView2 已在 create_window(transparent=True) 打开；本线程等窗口
        句柄就绪后启用 DWM Mica，成功则给前端注入 mica-on 类让页面底色让位
        系统背景。任一步失败（Win10/关闭桌面合成等）静默降级——前端无
        mica-on 类，保持原纯色背景，观感与旧版一致。"""
        import ctypes
        try:
            theme = ""
            try:
                cfg_path = Path(__file__).resolve().parent / "config.json"
                theme = str(json.loads(cfg_path.read_text(encoding="utf-8")).get("gui", {}).get("theme", ""))
            except Exception:
                pass  # 主题读取失败按浅色处理

            native = None
            for _ in range(60):
                native = getattr(window, "native", None)
                if native is not None and getattr(native, "Handle", 0):
                    break
                time.sleep(0.25)
            if native is None:
                logger.warning("Win11 背景：未获取窗口句柄，跳过 Mica")
                return

            hwnd = int(native.Handle.ToInt64()) if hasattr(native.Handle, 'ToInt64') else int(native.Handle)
            dwm = ctypes.windll.dwmapi
            dark = ctypes.c_int(1 if theme.lower() == "dark" else 0)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            backdrop = ctypes.c_int(2)                                  # DWMSBT_MAINWINDOW（Mica）
            hr = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), 4)
            if hr != 0:
                logger.warning(f"Win11 背景：Mica 不可用 (hr={hr})，保持纯色背景")
                return
            # 关键：WinForms 背景擦除会用不透明 BackColor 盖住 DWM Mica——
            # 把窗体刷子设为 alpha≈0（近透明），擦除等于不画，Mica 才能透出。
            # （alpha=1 而非 0：避开 Color.Transparent 的父背景特殊分支）
            from System.Drawing import Color  # clr 此时已由 pywebview 平台模块加载
            native.BackColor = Color.FromArgb(1, 0, 0, 0)
            window.evaluate_js("document.documentElement.classList.add('mica-on')")
            logger.info("Win11 背景：Mica 已启用")
        except Exception as e:
            logger.warning(f"Win11 背景：启用失败（保持纯色）: {e}")

    threading.Thread(target=_apply_win11_backdrop, args=(), daemon=True).start()

    # v0.2.0：首次启动后等页面加载完跳设置页（让用户填 API Key）
    if first_run_status == "NEEDS_API_KEY":
        def _jump_to_settings_when_ready():
            for _ in range(20):  # 最多等 5 秒
                time.sleep(0.25)
                try:
                    window.evaluate_js("location.hash = '#/settings'")
                    return
                except Exception:
                    continue
            logger.warning("跳设置页超时（WebView2 未就绪？）")
        threading.Thread(target=_jump_to_settings_when_ready, daemon=True).start()

    webview.start()
    # 窗口关闭后清理锁并退出
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass
    # H2b 修复：原直接 os._exit(0) 跳过所有 atexit、不 flush 缓冲区，会丢失最后几行日志、
    # 且硬杀仍在跑的 uvicorn 守护线程（在途写文件可能截断）。退出前先 logging.shutdown()
    # 刷盘所有 handler（含 analyzer.log 与 crash.log 的 RotatingFileHandler），再结束进程。
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
