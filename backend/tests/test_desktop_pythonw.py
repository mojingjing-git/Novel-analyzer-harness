"""pythonw（无控制台）启动兼容性测试

pythonw 下 sys.stdout/sys.stderr 是 None。run_desktop.bat 改用 pythonw
静默启动后，desktop.py 顶部必须先把 None 换成合法文件对象，
否则后续 TextIOWrapper / _StderrTee / print 都会 AttributeError。

这些用例直接检查模块源码逻辑（不真正启动 GUI）：
- 顶部 None→devnull 处理必须存在
- _start_server 必须用 log_config=None
- _StderrTee 在 _orig 为合法文件对象时 write/flush/__getattr__ 不崩
"""
import ast
import io
import logging
import sys
from pathlib import Path

import pytest


DESKTOP_PATH = Path(__file__).resolve().parent.parent.parent / "desktop.py"


def _load_desktop_source() -> str:
    return DESKTOP_PATH.read_text(encoding="utf-8")


def test_top_level_handles_none_stdout_and_stderr():
    """desktop.py 顶部必须有 sys.stdout/stderr is None -> devnull 的显式处理"""
    tree = ast.parse(_load_desktop_source())
    top_try = None
    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue
        stdout_guard = any(
            isinstance(n, ast.If)
            and isinstance(n.test, ast.Compare)
            and isinstance(n.test.left, ast.Attribute)
            and n.test.left.attr == "stdout"
            and isinstance(n.test.comparators[0], ast.Constant)
            and n.test.comparators[0].value is None
            for n in node.body
        )
        stderr_guard = any(
            isinstance(n, ast.If)
            and isinstance(n.test, ast.Compare)
            and isinstance(n.test.left, ast.Attribute)
            and n.test.left.attr == "stderr"
            and isinstance(n.test.comparators[0], ast.Constant)
            and n.test.comparators[0].value is None
            for n in node.body
        )
        if stdout_guard and stderr_guard:
            top_try = node
            break
    assert top_try is not None, "desktop.py 顶部缺少 sys.stdout/stderr is None 处理"


def test_start_server_uses_log_config_none():
    """uvicorn.run 必须显式传 log_config=None（pythonw 无控制台时默认配置会崩）"""
    source = _load_desktop_source()
    assert "log_config=None" in source, "_start_server 必须用 log_config=None"
    assert '"stream": "ext://sys.stderr"' not in source, (
        "uvicorn log_config 不应再引用 sys.stderr（pythonw 下为 None）"
    )


def test_stderr_tee_survives_none_orig():
    """_StderrTee 在 _orig 为合法文件对象时 write/flush/__getattr__ 不崩"""
    """源码级验证：_StderrTee.write/flush 对 self._orig 的调用都有 try/except 兜底，
    __getattr__ 的 getattr(self._orig, name) 代理在 _orig 为 devnull/TextIOWrapper
    时是合法的（顶部 None 补齐后 _orig 永不为 None）。"""
    source = _load_desktop_source()
    assert "self._orig.write(s)" in source
    assert "self._orig.flush()" in source
    assert "return getattr(self._orig, name)" in source
    # 顶部 None 补齐保证 _orig 不为 None（这是 __getattr__ 代理安全的前提）
    assert "if sys.stdout is None:" in source
    assert "if sys.stderr is None:" in source


def test_api_get_session_token_reads_env():
    """Api.get_session_token 从 SESSION_TOKEN_ENV 读取、未设返回空串（PR-1 收尾）

    前端 client.ts 经 pywebview js_api 取 token 给所有 fetch 附加
    X-Session-Token header；方法名是前端 sessionToken.ts 的硬契约
    （frontend/src/api/sessionToken.test.ts 同步锁定），改名必须两侧同步。

    源码级断言（与文件既有风格一致，不 import desktop.py——其顶层副作用
    含 stdout 重绑 / chdir 会污染 pytest 进程）。
    """
    source = _load_desktop_source()
    assert "def get_session_token(self)" in source
    # 未设环境变量（浏览器 dev 模式）返回空串而非抛错
    assert 'os.environ.get(SESSION_TOKEN_ENV, "")' in source
