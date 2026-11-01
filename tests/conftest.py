"""
顶层 tests/ 共享 pytest 配置

注册自定义 marker + 让 pytest 在顶层 tests/ 也能收集到测试。
"""
import sys
from pathlib import Path

# 让 tests/test_splitter_*.py 能 `from backend.xxx import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    """注册所有自定义 marker。"""
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    )
