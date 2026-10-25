"""Step0/R0 守卫：pytest 运行根隔离必须生效（含独立子进程回归）。

R0 第一轮被拒原因：原实现先 import ``backend.core.llm_failure_logger``（此时模块顶层
``FailureLogger()`` 已按真实 cwd 创建/打开 ``api_failures.log``），之后才移除 handler，
属于「写后移除」；测试内部新建 ``FailureLogger()`` 也会按真实 cwd 重建 handler 并写入。
修正方式：rootdir ``conftest.py`` 顶层先把测试进程 cwd 切到隔离根。

本文件分两层验证：

1. 当前进程：cwd / ``NOVEL_ROOT`` / 关键路径 / 合成配置都必须指向隔离根。
2. 独立子进程：用临时目录模拟「原始 cwd」，加载隔离代码后验证「首次 import」与
   「用例内新建 ``FailureLogger()`` + ``record_failure``」都不会在原始 cwd 创建/改动
   默认日志，且实际 handler 只指向隔离根。子进程的两个场景分别为
   「原始 cwd 无默认日志」与「原始 cwd 存在 synthetic sentinel」。

真实仓库文件只做存在性 / 哈希核验，测试不读取或改写真实日志内容。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFTEST_PATH = REPO_ROOT / "conftest.py"


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 进程内契约
# ---------------------------------------------------------------------------

def test_process_cwd_is_isolation_root():
    root = Path(os.environ["NOVEL_ROOT"])
    assert "novel-analyzer-pytests-" in root.name
    assert _norm(Path.cwd()) == _norm(root), (
        f"测试进程 cwd 必须落在隔离根: cwd={Path.cwd()} isolation_root={root}"
    )


def test_novel_root_points_to_isolation_root():
    novel_root = os.environ.get("NOVEL_ROOT")
    assert novel_root, "conftest 必须在任何 backend import 前设置 NOVEL_ROOT"
    assert "novel-analyzer-pytests-" in Path(novel_root).name, (
        f"NOVEL_ROOT 应指向隔离临时根，实际 {novel_root}"
    )


def test_queue_manager_paths_isolated():
    from backend.services import queue_manager

    root = Path(os.environ["NOVEL_ROOT"]).resolve()
    assert queue_manager.PROJECT_ROOT == root
    assert queue_manager.CONFIG_FILE.parent == root
    assert queue_manager.QUEUE_STATE_FILE.parent == root


def test_analysis_service_reads_synthetic_config():
    from backend.services.queue_service import get_service

    root = Path(os.environ["NOVEL_ROOT"]).resolve()
    service = get_service()
    # 合成配置：model 固定非空、api_key 为空 —— 证明真实 config.json 未被读取
    assert service.config_manager.config.api.model == "test-model"
    assert service.config_manager.config.api.api_key == ""
    # workspace_path = NOVEL_ROOT/workspace，写盘目标不在真实仓库 workspace/
    assert service.workspace_path == root / "workspace"


def test_analyzer_log_file_isolated():
    import backend.app as app_module

    root = Path(os.environ["NOVEL_ROOT"]).resolve()
    assert app_module.PROJECT_ROOT == root
    assert app_module.LOG_FILE.parent == root


# ---------------------------------------------------------------------------
# 独立子进程回归：原始 cwd = 临时目录
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = textwrap.dedent(
    """
    import importlib.util
    import json
    import logging
    import os
    import sys
    from pathlib import Path

    repo_root = Path(sys.argv[1])
    conftest_path = Path(sys.argv[2])
    sys.path.insert(0, str(repo_root))

    spec = importlib.util.spec_from_file_location("novel_isolation_bootstrap", conftest_path)
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    isolation_root = Path(bootstrap.ISOLATION_ROOT)

    # (1) first import in the process: module-level FailureLogger() builds a handler
    import backend.core.llm_failure_logger as failure_logger_module

    # (2) re-instantiation inside a test body: default relative path must stay isolated
    extra = failure_logger_module.FailureLogger()
    extra.record_failure(1, 1, 0.1, "RetryNeeded", "isolation-probe")

    def norm(value):
        return os.path.normcase(os.path.realpath(str(value)))

    handlers = [
        getattr(handler, "baseFilename", "")
        for handler in logging.getLogger("api_failures").handlers
    ]
    outside = [
        handler
        for handler in handlers
        if handler and not norm(handler).startswith(norm(isolation_root) + os.sep)
    ]
    payload = {
        "isolation_root": str(isolation_root),
        "cwd": os.getcwd(),
        "handlers": handlers,
        "handlers_outside": outside,
    }
    print("RESULT_JSON=" + json.dumps(payload, ensure_ascii=True))
    """
)


def _run_isolation_child(original_cwd: Path) -> dict:
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("NOVEL_ROOT", "LLM_API_KEY", "MINIMAX_API_KEY",
                       "NOVEL_ANALYZER_SESSION_TOKEN")
    }
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD_SCRIPT,
            str(REPO_ROOT),
            str(CONFTEST_PATH),
        ],
        cwd=str(original_cwd),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"隔离子进程失败（rc={proc.returncode}）\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    lines = [
        line[len("RESULT_JSON="):]
        for line in proc.stdout.splitlines()
        if line.startswith("RESULT_JSON=")
    ]
    assert lines, f"子进程未返回结果: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return json.loads(lines[-1])


def test_child_does_not_create_default_log_in_original_cwd(tmp_path):
    """场景 A：原始 cwd 无默认日志 —— 隔离后不得被创建"""
    original_cwd = tmp_path / "original_cwd"
    original_cwd.mkdir()

    result = _run_isolation_child(original_cwd)

    assert not (original_cwd / "api_failures.log").exists(), (
        "子进程在原始 cwd 创建了默认 api_failures.log，隔离失败"
    )
    assert result["handlers"], "应存在指向隔离根的 api_failures handler"
    assert result["handlers_outside"] == [], (
        f"handler 指向隔离根之外: {result['handlers_outside']}"
    )
    assert _norm(result["cwd"]) == _norm(result["isolation_root"]), (
        f"子进程 cwd 未落在隔离根: cwd={result['cwd']} root={result['isolation_root']}"
    )


def test_child_preserves_sentinel_log_in_original_cwd(tmp_path):
    """场景 B：原始 cwd 存在 synthetic sentinel —— 必须逐字节不变"""
    original_cwd = tmp_path / "original_cwd"
    original_cwd.mkdir()
    sentinel = original_cwd / "api_failures.log"
    sentinel.write_text("SENTINEL-DO-NOT-TOUCH\n", encoding="utf-8")
    before = sentinel.read_bytes()

    result = _run_isolation_child(original_cwd)

    assert sentinel.read_bytes() == before, "原始 cwd 的 sentinel 日志被改写"
    assert result["handlers_outside"] == [], (
        f"handler 指向隔离根之外: {result['handlers_outside']}"
    )


def test_child_does_not_touch_repo_default_log(tmp_path):
    """端到端：子进程运行前后，真实仓库 api_failures.log 哈希不变（只做哈希核验）"""
    repo_log = REPO_ROOT / "api_failures.log"
    if not repo_log.exists():
        return
    before = _sha256(repo_log)

    original_cwd = tmp_path / "original_cwd"
    original_cwd.mkdir()
    _run_isolation_child(original_cwd)

    assert _sha256(repo_log) == before, "隔离子进程改写了真实仓库 api_failures.log"


# ---------------------------------------------------------------------------
# 正向对照：校验器本身必须能抓到真实泄漏（避免 nul/设备排除变成放行旁路）
# ---------------------------------------------------------------------------

def test_isolation_check_flags_real_outside_handler(tmp_path):
    """api_failures 挂上隔离根之外的真实文件 handler 时，校验必须抛错"""
    import pytest_isolation as isolation_bootstrap

    outside_file = tmp_path / "outside_api_failures.log"
    import logging
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(str(outside_file), encoding="utf-8",
                                  maxBytes=1024, backupCount=1)
    flog = logging.getLogger("api_failures")
    flog.addHandler(handler)
    try:
        detected = isolation_bootstrap.api_failures_handlers_outside_isolation()
        assert any(_norm(item) == _norm(outside_file) for item in detected), (
            f"校验器未抓到隔离根之外的真实 handler: {detected}"
        )
        try:
            isolation_bootstrap.assert_isolation_intact()
        except RuntimeError:
            pass
        else:
            raise AssertionError("存在真实泄漏时 assert_isolation_intact 未报错")
    finally:
        flog.removeHandler(handler)
        handler.close()

    isolation_bootstrap.assert_isolation_intact()


def test_isolation_check_tolerates_null_device_handler():
    """空设备 handler（pytest logging 插件所挂）必须被排除，不算泄漏"""
    import pytest_isolation as isolation_bootstrap

    assert isolation_bootstrap._is_null_device(os.devnull)
    assert isolation_bootstrap._is_null_device("\\\\.\\nul")
    assert not isolation_bootstrap._is_null_device(
        isolation_bootstrap.ISOLATION_ROOT / "api_failures.log"
    )
