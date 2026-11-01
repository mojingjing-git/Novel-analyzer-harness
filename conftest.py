"""pytest 运行根隔离（第三批清理 Step0/R0，2026-10-14；R0 第二轮修正）

R0 第一轮被拒原因（主审复现）：原实现是「写后移除」——先 import
``backend.core.llm_failure_logger``，模块顶层 ``FailureLogger()`` 已按真实 cwd
创建/打开 ``api_failures.log``，之后才移除 handler；测试内部再 ``FailureLogger()``
也会按真实 cwd 重建 handler 并写入。

修正：在本文件顶层、任何 ``backend.*`` import 之前，把测试进程 cwd 切到会话级隔离根
（实现见 :mod:`pytest_isolation`），使「默认相对路径」在首次 import、模块顶层实例化
与用例内任意重建实例时都落在隔离根。

测试代码与 fixture 路径全部按 ``__file__`` 绝对定位，不依赖 cwd。
"""

from __future__ import annotations

from pathlib import Path

import pytest_isolation

REPO_ROOT = Path(__file__).resolve().parent
pytest_isolation.install(REPO_ROOT)

ISOLATION_ROOT = pytest_isolation.ISOLATION_ROOT

# SSL context 缓存：见 pytest_ssl_cache.py 的说明。
# 本机 load_verify_locations(certifi) 要 3.8 秒，而 httpx 每个客户端建 2 个 context，
# 全量测试 78 处客户端构造把 750 秒几乎全耗在这里。缓存后每个客户端 0.001 秒。
import pytest_ssl_cache  # noqa: E402

pytest_ssl_cache.install_ssl_cache()


import pytest  # noqa: E402  (隔离安装后再 import，保持文件意图清晰)


@pytest.fixture(autouse=True)
def _reset_runtime_singletons():
    pytest_isolation.assert_isolation_intact()
    pytest_isolation.assert_proxy_env_sane()
    yield
    pytest_isolation.assert_isolation_intact()
    pytest_isolation.assert_proxy_env_sane()
    pytest_isolation.reset_runtime_singletons()
