"""pytest 运行根隔离原语（第三批清理 Step0/R0，2026-10-14）

为什么单独成模块：rootdir 下有两个 ``conftest.py``（仓库根与 ``tests/``），
``import conftest`` 会随收集范围不同而解析到不同模块对象，测试无法稳定访问隔离原语。
本模块由仓库根 ``conftest.py`` 在 import 期调用 :func:`install`，测试直接
``import pytest_isolation`` 取用同一份状态。

隔离策略（写前隔离，不是写后移除）：在任何 ``backend.*`` import 之前把测试进程 cwd
切到会话级隔离根，使「默认相对路径」在首次 import、模块顶层实例化以及用例内任意
重建实例时都落在隔离根。

覆盖的默认相对路径写入点：

- ``backend/core/llm_failure_logger.py``：``FailureLogger()`` 默认按 cwd 建
  ``api_failures.log`` handler（模块顶层即实例化一次）；
- ``backend/app.py``：``LOG_FILE`` 由 ``NOVEL_ROOT``/PROJECT_ROOT 求值并立即挂
  ``RotatingFileHandler``；
- ``backend/services/queue_manager.py``：``CONFIG_FILE`` / ``QUEUE_STATE_FILE``
  由 PROJECT_ROOT 求值，``save_queue()`` 直接覆写。
"""

from __future__ import annotations

import atexit
import configparser
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

SECRET_ENV_KEYS = ("LLM_API_KEY", "MINIMAX_API_KEY", "NOVEL_ANALYZER_SESSION_TOKEN")
SYNTHETIC_MODEL = "test-model"

# 由 install() 填充
REPO_ROOT: Path
ORIGINAL_CWD: Path
ISOLATION_ROOT: Path
ORIGINAL_SECRET_ENV_PRESENT: dict[str, bool]
TESTPATH_PLACEHOLDERS: list[str]

_installed = False


def _read_testpaths(repo_root: Path) -> list[str]:
    """读取 pytest.ini 的 testpaths（相对仓库根）。

    pytest 在 rootdir conftest 被 import 之后立刻执行 ``Config._decide_args``，
    其中 ``testpaths`` 用 ``glob.iglob(path)`` **按当前 cwd** 解析。我们已把 cwd 切到
    隔离根，若不建同名占位目录会 glob 失败并回退成「从 invocation_dir 递归收集」，
    把收集范围扩大到 workspace/ 等目录。建占位后 glob 正常返回，随后 pytest 用
    ``invocation_params.dir``（仓库根）解析这些相对参数，仍指向真实测试目录。
    """
    ini_path = repo_root / "pytest.ini"
    if not ini_path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(ini_path, encoding="utf-8")
        raw = parser.get("pytest", "testpaths", fallback="")
    except Exception as exc:  # 配置不可解析即可诊断失败，不静默继续
        raise RuntimeError(f"无法读取 {ini_path} 的 testpaths: {exc}") from exc
    return raw.split()


def install(repo_root: Path) -> Path:
    """建立运行根隔离。必须在任何 backend.* import 之前调用，且只调用一次。"""
    global _installed, REPO_ROOT, ORIGINAL_CWD, ISOLATION_ROOT
    global ORIGINAL_SECRET_ENV_PRESENT, TESTPATH_PLACEHOLDERS
    if _installed:
        return ISOLATION_ROOT

    REPO_ROOT = Path(repo_root).resolve()
    ORIGINAL_CWD = Path.cwd()
    ORIGINAL_SECRET_ENV_PRESENT = {
        key: bool(os.environ.get(key)) for key in SECRET_ENV_KEYS
    }

    ISOLATION_ROOT = Path(tempfile.mkdtemp(prefix="novel-analyzer-pytests-")).resolve()

    # 1) cwd 先切到隔离根：此后任何「默认相对路径」写入都落在隔离根
    os.chdir(ISOLATION_ROOT)
    os.environ["NOVEL_ROOT"] = str(ISOLATION_ROOT)

    # 2) 真实密钥 / 会话 token 不得进入测试进程
    for _secret_key in SECRET_ENV_KEYS:
        os.environ.pop(_secret_key, None)

    # 3) 合成配置：只有占位值，永不读取真实 config.json
    (ISOLATION_ROOT / "workspace").mkdir(parents=True, exist_ok=True)
    (ISOLATION_ROOT / "config.json").write_text(
        json.dumps(
            {
                "api": {
                    "base_url": "https://example.invalid/v1",
                    "api_key": "",
                    "model": SYNTHETIC_MODEL,
                    "provider": "auto",
                },
                "analysis": {},
                "gui": {},
                "workspace_dir": "workspace",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 4) testpaths 占位：保持收集范围等同于仓库配置
    TESTPATH_PLACEHOLDERS = _read_testpaths(REPO_ROOT)
    for _testpath in TESTPATH_PLACEHOLDERS:
        (ISOLATION_ROOT / _testpath).mkdir(parents=True, exist_ok=True)

    atexit.register(cleanup)
    _installed = True
    return ISOLATION_ROOT


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _within(path, root: Path) -> bool:
    p = _norm(path)
    r = _norm(root)
    return p == r or p.startswith(r + os.sep)


def _is_null_device(path_str) -> bool:
    """判断是否为 NUL / /dev/null 之类的空设备。

    pytest 的 logging 插件会给 ``propagate=False`` 的 logger 挂一个写向
    ``os.devnull`` 的 ``_FileHandler``（Windows 下 baseFilename 形如 ``\\.\\nul``）；
    这类 handler 不落真实文件，不计入泄漏。
    """
    lowered = str(path_str).replace("/", "\\").lower()
    return lowered.endswith("\\nul") or lowered in ("nul", "\\\\.\\nul", "\\\\?\\nul")


def api_failures_handlers_outside_isolation() -> list[str]:
    """返回指向隔离根之外真实文件的 api_failures file handler 路径（应为空）。"""
    outside = []
    for handler in logging.getLogger("api_failures").handlers:
        base = getattr(handler, "baseFilename", "")
        if not base or _is_null_device(base):
            continue
        if not _within(base, ISOLATION_ROOT):
            outside.append(base)
    return outside


def assert_isolation_intact() -> None:
    """校验隔离仍然成立；不成立直接抛可诊断错误，不静默修补、不吞异常。"""
    cwd = Path.cwd()
    if _norm(cwd) != _norm(ISOLATION_ROOT):
        raise RuntimeError(
            "测试运行根隔离失败：进程 cwd 已偏离隔离根。"
            f"cwd={cwd} isolation_root={ISOLATION_ROOT}。"
            "请检查是否有测试直接 chdir 未恢复。"
        )
    outside = api_failures_handlers_outside_isolation()
    if outside:
        raise RuntimeError(
            "测试运行根隔离失败：api_failures handler 指向隔离根之外 "
            f"(isolation_root={ISOLATION_ROOT}): {outside}"
        )


def cleanup() -> None:
    """退出清理：先 chdir 出隔离根，再关句柄，再删目录。"""
    try:
        if _norm(Path.cwd()) == _norm(ISOLATION_ROOT):
            os.chdir(ORIGINAL_CWD)
    except Exception:
        pass
    for logger_obj in (logging.getLogger(), logging.getLogger("api_failures")):
        for handler in list(logger_obj.handlers):
            base = getattr(handler, "baseFilename", "")
            if base and _within(base, ISOLATION_ROOT):
                try:
                    handler.close()
                except Exception:
                    pass
    shutil.rmtree(ISOLATION_ROOT, ignore_errors=True)


def reset_runtime_singletons() -> None:
    """逐用例重置进程级单例与模块缓存，避免跨用例污染。"""
    queue_service = sys.modules.get("backend.services.queue_service")
    if queue_service is not None:
        queue_service._service = None
    book_service = sys.modules.get("backend.services.book_service")
    if book_service is not None:
        book_service._books.clear()
        book_service._manual_books.clear()
        book_service._refreshing = False
    location_service = sys.modules.get("backend.services.location_normalization_service")
    if location_service is not None:
        location_service._service = None
    summary_service = sys.modules.get("backend.services.summary_service")
    if summary_service is not None:
        summary_service._service = None


import sys  # noqa: E402  (与 REPO_ROOT 求值相邻的副作用导入，保持此处可读)
