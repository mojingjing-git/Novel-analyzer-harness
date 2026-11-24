"""关键运行时依赖健康检查（纯标准库封装，可由 Web 服务与 CLI 共用）。"""

from __future__ import annotations

from typing import Any, Dict


def check_dependency_health() -> Dict[str, Any]:
    """检查关键依赖是否可正常导入。

    Returns:
        dict: ``{"status": "ok" | "degraded" | "critical", "deps": ..., "missing": ..., "critical_missing": ...}``
    """
    deps = [
        ("json5", "json5", True),
        ("json_repair", "json_repair", True),
        ("openai", "openai", True),
        ("anthropic", "anthropic", False),
        ("openpyxl", "openpyxl", True),
        ("websockets", "websockets", True),
    ]
    health: Dict[str, Any] = {
        "status": "ok",
        "deps": {},
        "missing": [],
        "critical_missing": [],
    }
    for label, module, critical in deps:
        try:
            m = __import__(module)
            health["deps"][label] = {
                "status": "ok",
                "version": getattr(m, "__version__", "unknown"),
            }
        except ImportError as e:
            health["deps"][label] = {"status": "missing", "error": str(e)}
            health["missing"].append(label)
            if critical:
                health["critical_missing"].append(label)
    if health["critical_missing"]:
        health["status"] = "critical"
    elif health["missing"]:
        health["status"] = "degraded"
    return health
