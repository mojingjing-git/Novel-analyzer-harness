"""冒烟、架构分层与运行隔离基础测试。"""
from __future__ import annotations

import importlib
import os
import pkgutil
import re
from pathlib import Path

import pytest

import backend
from backend.config import paths
from backend.core import model_catalog
from backend.core.llm_failure_logger import failure_logger
from backend.tests import pytest_isolation

BACKEND_DIR = Path(__file__).resolve().parents[1]
SKIP_PREFIXES = ("backend.tests",)

FORBIDDEN_IN_COMMON = re.compile(
    r"^\s*(from|import)\s+(backend\.(core|services)|\.{2,}(core|services)\b)",
    re.MULTILINE,
)
FORWARDED_MODULES = ("error_types.py", "error_taxonomy.py", "moderation.py")
FORBIDDEN_IN_API = re.compile(
    r"^\s*from\s+backend\.core\.(file_processor|knowledge_base|prompt_builder|pipeline)"
    r"|^\s*from\s+backend\.services\.(final_summary|recheck_v2|recheck_v3|recheck_resolution)",
    re.MULTILINE,
)


def _discover_modules() -> list[str]:
    names: list[str] = ["backend"]

    def walk(prefix: str, path: list[str]) -> None:
        for mod in pkgutil.iter_modules(path, prefix):
            names.append(mod.name)
            if not mod.ispkg:
                continue
            try:
                pkg = importlib.import_module(mod.name)
            except Exception:
                continue
            sub_path = getattr(pkg, "__path__", None)
            if sub_path:
                walk(mod.name + ".", list(sub_path))

    walk("backend.", list(backend.__path__))
    return [n for n in names if not n.startswith(SKIP_PREFIXES)]


MODULES = _discover_modules()


def test_all_production_modules_importable() -> None:
    for pkg in ("backend.api", "backend.common", "backend.core", "backend.services",
                "backend.utils", "backend.models", "backend.config"):
        assert pkg in MODULES, f"冒烟发现机制漏掉了子包 {pkg}"
    for modname in MODULES:
        importlib.import_module(modname)


def _py_sources(rel_dir: str) -> dict[str, str]:
    pkg = BACKEND_DIR / rel_dir
    return {
        p.name: p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(pkg.glob("*.py"))
        if p.name != "__init__.py"
    }


def test_architecture_layer_boundaries() -> None:
    for name, text in _py_sources("common").items():
        assert FORBIDDEN_IN_COMMON.search(text) is None, f"common/{name} 引入了上层依赖"

    for name in FORWARDED_MODULES:
        text = (BACKEND_DIR / "core" / name).read_text(encoding="utf-8", errors="ignore")
        assert "from ..common" in text, f"core/{name} 应转发 backend/common"

    for name, text in _py_sources("api").items():
        assert FORBIDDEN_IN_API.search(text) is None, f"api/{name} 直接编排业务引擎"


def test_runtime_isolation_active() -> None:
    pytest_isolation.assert_isolation_intact()
    assert Path.cwd().resolve() == pytest_isolation.ISOLATION_ROOT
    assert os.environ.get("NOVEL_ROOT") == str(pytest_isolation.ISOLATION_ROOT)
    assert os.environ.get("NOVEL_RUNTIME_DIR") == str(pytest_isolation.ISOLATION_ROOT)
    assert paths.get_runtime_dir() == pytest_isolation.ISOLATION_ROOT
    assert pytest_isolation._within(failure_logger.log_file, pytest_isolation.ISOLATION_ROOT)
    assert pytest_isolation._within(model_catalog._resolve_user_data_dir(), pytest_isolation.ISOLATION_ROOT)
    assert pytest_isolation.normalize_no_proxy("localhost,[::1],127.0.0.1") == "localhost,::1,127.0.0.1"
