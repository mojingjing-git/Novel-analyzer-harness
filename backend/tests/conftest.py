"""pytest 运行根隔离（会话级隔离根：先切 cwd/环境，再加载业务 backend 模块）。

``backend/__init__.py`` 与 ``backend/tests/__init__.py`` 均为空文件，因此在本文件
顶部导入 ``backend.tests.pytest_isolation`` 不会触发任何 ``backend.app`` / ``backend.core`` /
``backend.services`` 模块的副作用。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.tests import pytest_isolation  # noqa: E402

pytest_isolation.install(REPO_ROOT)

ISOLATION_ROOT = pytest_isolation.ISOLATION_ROOT

from backend.tests import pytest_ssl_cache  # noqa: E402

pytest_ssl_cache.install_ssl_cache()

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_runtime_singletons():
    pytest_isolation.assert_isolation_intact()
    pytest_isolation.assert_proxy_env_sane()
    yield
    pytest_isolation.assert_isolation_intact()
    pytest_isolation.assert_proxy_env_sane()
    pytest_isolation.reset_runtime_singletons()
