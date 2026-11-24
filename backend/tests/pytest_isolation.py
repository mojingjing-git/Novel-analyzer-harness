"""pytest 运行根隔离原语（会话级隔离根 + 代理环境净化）。

由 ``backend/tests/conftest.py`` 在任何业务 ``backend.*`` import 之前调用 :func:`install`，
将进程 cwd、``NOVEL_ROOT`` 与 ``NOVEL_RUNTIME_DIR`` 统一切到临时隔离根，确保测试全程
对仓库和 ``runtime/`` 零写入，同时保留 ``TESTPATH_PLACEHOLDERS`` 兼容无参 ``pytest`` 调用。
"""

from __future__ import annotations

import atexit
import configparser
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from backend.common.proxy_env import (
    PROXY_ENV_KEYS,
    PROXY_SANITIZATION,
    assert_proxy_env_sane,
    normalize_no_proxy,
    sanitize_proxy_env,
)

__all__ = [
    "PROXY_ENV_KEYS",
    "PROXY_SANITIZATION",
    "assert_proxy_env_sane",
    "normalize_no_proxy",
    "sanitize_proxy_env",
    "install",
    "assert_isolation_intact",
    "api_failures_handlers_outside_isolation",
    "cleanup",
    "reset_runtime_singletons",
]

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
    """读取 pytest.ini 的 testpaths（相对仓库根），在隔离根创建占位目录避免二次收集回退全仓扫描。"""
    ini_path = repo_root / "pytest.ini"
    if not ini_path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(ini_path, encoding="utf-8")
        raw = parser.get("pytest", "testpaths", fallback="")
    except Exception as exc:
        raise RuntimeError(f"无法读取 {ini_path} 的 testpaths: {exc}") from exc
    return raw.split()


def install(repo_root: Path) -> Path:
    """建立运行根隔离。必须在任何业务 backend.* import 之前调用，且只调用一次。"""
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

    # 0) 代理环境净化：早于 httpx 客户端构造
    sanitize_proxy_env()

    # 1) cwd 与运行根双隔离：任意默认相对路径或 NOVEL_RUNTIME_DIR 写入都落在隔离根
    os.chdir(ISOLATION_ROOT)
    os.environ["NOVEL_ROOT"] = str(ISOLATION_ROOT)
    os.environ["NOVEL_RUNTIME_DIR"] = str(ISOLATION_ROOT)

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

    # 4) testpaths 占位：保证无参 pytest 二次收集不会回退扫描全仓
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
    """校验隔离仍然成立；不成立直接抛可诊断错误。"""
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
    summary_service = sys.modules.get("backend.services.summary_service")
    if summary_service is not None:
        summary_service._service = None
