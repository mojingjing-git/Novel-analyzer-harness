"""SSL context 缓存（测试环境专用）。

**为什么需要**：`httpx` 每次构造客户端都会调 `ssl.create_default_context(cafile=certifi.where())`，
其内部 `load_verify_locations` 在本机耗时较长，且 httpx 一个客户端会建两个 SSL context。
在测试短生命周期进程内复用只读的默认 SSLContext，避免数十处客户端构造重复解析证书链。

**实现方式**：只缓存 ``verify=True`` 且 ``cert=None`` 的默认上下文，幂等覆盖
``httpx._config`` 与 ``httpx._transports.default`` 两处调用点。
"""

from __future__ import annotations

import ssl
from typing import Any


_INSTALLED = False
_CACHE: dict[Any, ssl.SSLContext] = {}


def install_ssl_cache() -> bool:
    """在当前进程内缓存 httpx 的默认 SSLContext。返回是否安装成功（幂等）。"""
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import httpx._config as hc
        import httpx._transports.default as hd
    except Exception:  # pragma: no cover - httpx 结构变化时静默跳过
        return False

    original = getattr(hc, "create_ssl_context", None)
    if original is None:  # pragma: no cover
        return False

    def cached(verify=True, cert=None, trust_env=True):
        if verify is True and cert is None:
            ctx = _CACHE.get("default")
            if ctx is None:
                ctx = original(verify=verify, cert=cert, trust_env=trust_env)
                _CACHE["default"] = ctx
            return ctx
        return original(verify=verify, cert=cert, trust_env=trust_env)

    hc.create_ssl_context = cached
    hd.create_ssl_context = cached
    _INSTALLED = True
    return True
