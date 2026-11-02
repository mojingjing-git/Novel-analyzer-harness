"""架构守护：分层依赖规则（不依赖运行时行为，只审源码文本）

锁住三条规则：

1. **vnext 禁止反向依赖 1.x**：backend/vnext 下不得出现 backend.core /
   backend.services 的 import（vnext/citations.py 是唯一白名单——顶层转发
   core.citations 的公开 API，docstring 已声明有意为之）。
2. **common 只依赖标准库与自身**：backend/common 下不得 import core /
   services / vnext，它是两层引擎共用的最底层纯逻辑语义层。
3. **core 的错误分类三件套是转发层**：error_types / error_taxonomy / moderation
   的权威实现在 common，core 侧只保留兼容转发——防止有人在 core 重新实现
   造成两份权威。
"""
from __future__ import annotations

import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

FORBIDDEN_IN_VNEXT = re.compile(
    r"^\s*(from|import)\s+(backend\.core|backend\.services|\.{1,2}\.?(core|services)\b)",
    re.MULTILINE,
)
FORBIDDEN_IN_COMMON = re.compile(
    r"^\s*(from|import)\s+(backend\.(core|services|vnext)|\.{2,}(core|services|vnext)\b)",
    re.MULTILINE,
)
# 纯转发垫片白名单：顶层转发 core 公开 API 的兼容层，各自 docstring 已声明转发语义。
# 准入标准：文件只含转发 import（无实现逻辑），且权威实现在 core 或 common。
VNEXT_FORWARD_WHITELIST = {"citations.py", "prompt_limits.py"}

FORWARDED_MODULES = ("error_types.py", "error_taxonomy.py", "moderation.py")


def _py_sources(rel_dir: str) -> dict[str, str]:
    pkg = BACKEND_DIR / rel_dir
    return {
        p.name: p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(pkg.glob("*.py"))
        if p.name != "__init__.py"
    }


def test_vnext_does_not_import_core_or_services() -> None:
    for name, text in _py_sources("vnext").items():
        if name in VNEXT_FORWARD_WHITELIST:
            continue
        hit = FORBIDDEN_IN_VNEXT.search(text)
        assert hit is None, (
            f"vnext/{name} 反向依赖 1.x：{hit.group(0).strip()!r}。"
            "共享逻辑应下沉 backend/common（core 侧留兼容转发）。"
        )


def test_common_depends_only_on_stdlib_and_itself() -> None:
    for name, text in _py_sources("common").items():
        hit = FORBIDDEN_IN_COMMON.search(text)
        assert hit is None, f"common/{name} 引入了上层依赖：{hit.group(0).strip()!r}"


def test_core_taxonomy_modules_are_forwarding_shims() -> None:
    for name in FORWARDED_MODULES:
        text = (BACKEND_DIR / "core" / name).read_text(encoding="utf-8", errors="ignore")
        assert "from ..common" in text, (
            f"core/{name} 应是 backend/common 的兼容转发层；"
            "若你在 core 里重新实现判定逻辑，请改到 common（权威）并让 core 转发。"
        )


def test_common_is_importable_and_identity_stable() -> None:
    """转发必须保证符号身份唯一：core 与 common 拿到的是同一个 ErrorClass 对象。"""
    from backend.common import error_taxonomy as common_tax
    from backend.common import error_types as common_et
    from backend.core import error_types as core_et
    from backend.core import error_taxonomy as core_tax

    assert core_et.ErrorClass is common_et.ErrorClass
    assert core_tax.classify_exception is not None
    # 同一输入经两条路径判定，结论必须是同一个枚举成员（is 语义）
    v = core_tax.classify_message("insufficient_quota")
    assert v.cls is common_tax.classify_message("insufficient_quota").cls
