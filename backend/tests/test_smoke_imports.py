"""冒烟：backend 全部生产模块可导入（自动发现，不维护清单）

T0 档的入口用例。用 pkgutil 递归列出 backend 下的所有模块并逐个 import——
新加的生产模块自动进入冒烟覆盖，不需要改这里。导入失败（语法错误、顶层异常、
依赖缺失）在这条用例上暴露，而不是等到某个功能测试才炸。

只跳过 ``backend.tests`` 自身：测试模块不是生产面，且部分依赖 conftest fixture。
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest

import backend

SKIP_PREFIXES = ("backend.tests",)


def _discover() -> list[str]:
    """递归列出 backend 包下所有模块与子包名（含 backend 本身）。"""
    names: list[str] = []

    def walk(prefix: str, path: list[str]) -> None:
        for mod in pkgutil.iter_modules(path, prefix):
            names.append(mod.name)
            if not mod.ispkg:
                continue
            try:
                pkg = importlib.import_module(mod.name)
            except Exception:
                # 子包导入失败本身会被下面的参数化用例标红；这里停止下钻即可
                continue
            sub_path = getattr(pkg, "__path__", None)
            if sub_path:
                walk(mod.name + ".", list(sub_path))

    names.append("backend")
    walk("backend.", list(backend.__path__))
    return [n for n in names if not n.startswith(SKIP_PREFIXES)]


MODULES = _discover()


def test_discovery_covers_production_packages() -> None:
    """发现机制本身必须看到 backend 的各个生产子包。"""
    for pkg in ("backend.api", "backend.core", "backend.services",
                "backend.vnext", "backend.utils", "backend.models", "backend.config"):
        assert pkg in MODULES, f"冒烟发现机制漏掉了子包 {pkg}"


@pytest.mark.parametrize("modname", MODULES)
def test_module_imports(modname: str) -> None:
    importlib.import_module(modname)
