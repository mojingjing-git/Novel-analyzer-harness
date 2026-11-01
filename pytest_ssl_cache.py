"""SSL context 缓存（测试环境专用）。

**为什么需要**：`httpx` 每次构造客户端都会调 `ssl.create_default_context(cafile=certifi.where())`，
其内部 `load_verify_locations` 在本机要 **3.8 秒**（`set_default_verify_paths` 只要 0.000 秒，
说明慢的是逐个解析 certifi 的 272KB PEM 证书链）。而 httpx 一个客户端要建 **两个** SSL context
（主传输 + 代理传输），于是每 `LLMClient(...)` / `AsyncOpenAI(...)` 构造一次就是 10–25 秒。

全量测试里有 78 处这样的构造，750 秒里绝大部分是它——不是测试逻辑慢。

**怎么修**：SSLContext 只读、可共享，所以在进程内缓存一份，让所有客户端复用。
首次构建仍有 3.8 秒一次性成本，之后每个客户端 0.001 秒。

**为什么只在测试环境做**：生产里 `LLMClient` 是长驻单例，构造次数极少，不值得引入
全局可变状态；而且这里 patch 的是 httpx 私有模块路径，跨 httpx 版本有风险。
测试环境是短生命周期进程，patch 安全且收益最大。

**跨版本兼容**：`httpx._transports.default` 是 `from .._config import create_ssl_context`，
所以必须同时打 `_config`（源头）和 `_transports.default`（实际调用点）两处；
任一路径不存在就跳过，不让测试因为 httpx 升级而炸。
"""

from __future__ import annotations

import ssl
from typing import Any


_INSTALLED = False
_CACHE: dict[Any, ssl.SSLContext] = {}


def install_ssl_cache() -> bool:
    """在当前进程内缓存 httpx 的默认 SSLContext。返回是否安装成功。

    幂等：重复调用只生效一次。
    """
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
        # 只缓存「默认信任链」这一种组合；自定义 verify/cert 仍走原实现
        if verify is True and cert is None:
            ctx = _CACHE.get("default")
            if ctx is None:
                ctx = original(verify=verify, cert=cert, trust_env=trust_env)
                _CACHE["default"] = ctx
            return ctx
        return original(verify=verify, cert=cert, trust_env=trust_env)

    # 源头 + 实际调用点都要打
    hc.create_ssl_context = cached
    hd.create_ssl_context = cached
    _INSTALLED = True
    return True
