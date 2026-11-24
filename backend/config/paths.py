"""运行时路径解析单一真源。

区分三类根目录：
- CODE_ROOT：Python 代码包所在根（run_desktop.bat 下优先指向本地 SSD 副本）；
- BUNDLE_DIR：只读嵌入式资源所在根（frontend/dist、desktop/ 模板、backend/data）；
- RUNTIME_DIR：可写运行时数据根目录（打包 exe 下为 EXE 同级，源码态默认为 <repo>/runtime）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_code_root(path: Path) -> bool:
    """用 backend/app.py 识别真实源码根，避免将测试占位目录误判为代码根。"""
    return (path / "backend" / "app.py").is_file()


def get_code_root() -> Path:
    """返回当前进程加载 backend 包的代码根目录。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return Path(getattr(sys, "_MEIPASS", exe_dir))
    local_root = Path(__file__).resolve().parents[2]
    if _is_code_root(local_root):
        return local_root
    novel_root = os.environ.get("NOVEL_ROOT", "").strip()
    if novel_root:
        candidate = Path(novel_root).resolve()
        if _is_code_root(candidate):
            return candidate
    return local_root


def get_bundle_dir() -> Path:
    """返回只读资源根目录（frontend/dist、desktop/ 模板与内置包数据）。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return Path(getattr(sys, "_MEIPASS", exe_dir))
    novel_root = os.environ.get("NOVEL_ROOT", "").strip()
    if novel_root:
        return Path(novel_root).resolve()
    return get_code_root()


def get_runtime_dir() -> Path:
    """返回运行时可写数据根目录。

    优先级：
    1. PyInstaller 打包 EXE：始终落在 EXE 同级目录（零 %APPDATA% 残留）。
    2. 显式环境变量 NOVEL_RUNTIME_DIR。
    3. 兼容环境变量 NOVEL_ROOT：若指向真实代码仓库根，落入 <NOVEL_ROOT>/runtime；
       若指向外置数据目录或测试隔离目录，直接使用该目录。
    4. 默认源码模式：<CODE_ROOT>/runtime。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    runtime_env = os.environ.get("NOVEL_RUNTIME_DIR", "").strip()
    if runtime_env:
        return Path(runtime_env).resolve()
    novel_root = os.environ.get("NOVEL_ROOT", "").strip()
    if novel_root:
        candidate = Path(novel_root).resolve()
        if _is_code_root(candidate):
            return candidate / "runtime"
        return candidate
    return get_code_root() / "runtime"


def ensure_runtime_dir() -> Path:
    """确保运行时数据根目录存在并返回该 Path。"""
    runtime_dir = get_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def runtime_path(*parts: str) -> Path:
    """构造位于运行时数据根目录下的路径。"""
    return get_runtime_dir().joinpath(*parts)
