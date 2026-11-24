"""API 失败日志记录器（FailureLogger）
专门记录 API 调用失败的详细信息，进程级单例经 _get_failure_logger() 获取。

用 RotatingFileHandler（10MB × 3）追加写入，轮转由 logging 体系自动管理，
与 analyzer.log/crash.log 策略一致。
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Union

from ..config.paths import ensure_runtime_dir

logger = logging.getLogger(__name__)


# 敏感凭证脱敏正则：
# 覆盖 OpenAI / Anthropic 风格 key、x-api-key / Authorization header 形式
# 显式长度 20+ 避免误伤短随机串；多模式 any-match 全部替换
# **顺序很关键**：sk-ant- 必须在 sk- 之前（否则会被通用 sk- 模式先吃掉）
_REDACT_PATTERNS = [
    (re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}'), 'sk-ant-***'),                    # Anthropic sk-ant-...（必须先匹配）
    (re.compile(r'sk-[A-Za-z0-9_-]{20,}'), 'sk-***'),                            # OpenAI sk-...
    (re.compile(r'(x-api-key:\s*)\S+', re.IGNORECASE), r'\1***'),                # x-api-key header
    (re.compile(r'(Authorization:\s*Bearer\s+)\S+', re.IGNORECASE), r'\1***'),    # Authorization Bearer
]


def redact(text: str) -> str:
    """对文本中的敏感凭证（API key / token）做脱敏。

    - 覆盖：OpenAI sk-... / Anthropic sk-ant-... / x-api-key header / Authorization: Bearer
    - 显式长度 20+ 避免误伤短随机串
    - **fail-open**：re.sub 抛异常时返回原文（宁可漏脱敏也不丢日志）
    """
    if not text:
        return text
    try:
        for pattern, replacement in _REDACT_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    except Exception:
        # fail-open：避免 redact 失败导致日志丢失
        return text


class FailureLogger:
    """失败日志记录器 - 专门记录API调用失败的详细信息（线程安全）"""

    def __init__(self, log_file: Optional[Union[str, Path]] = None):
        if log_file is None:
            self.log_file = ensure_runtime_dir() / "api_failures.log"
        else:
            self.log_file = Path(log_file)
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.failures = []
        self._lock = threading.Lock()
        self._logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        """构造带 RotatingFileHandler(10MB × 3) 的专用 logger，避免与别处句柄冲突。"""
        flog = logging.getLogger("api_failures")
        flog.setLevel(logging.INFO)
        flog.propagate = False
        target = os.path.normcase(os.path.abspath(str(self.log_file)))
        if not any(
            getattr(h, "baseFilename", "")
            and os.path.normcase(os.path.abspath(getattr(h, "baseFilename", ""))) == target
            for h in flog.handlers
        ):
            rfh = RotatingFileHandler(
                str(self.log_file), encoding="utf-8",
                maxBytes=10 * 1024 * 1024, backupCount=3,
            )
            # 简洁格式：每行一条 JSON 记录，便于外部脚本直接 grep/jq
            rfh.setFormatter(logging.Formatter("%(message)s"))
            flog.addHandler(rfh)
        return flog

    def record_failure(
        self,
        attempt_num: int,
        max_retries: int,
        temperature: float,
        error_type: str,
        error_message: str,
        wait_time: float = 0,
        messages_length: int = 0
    ):
        """记录一次失败（线程安全）"""
        failure = {
            "timestamp": datetime.now().isoformat(),
            "attempt": f"{attempt_num}/{max_retries}",
            "temperature": temperature,
            "error_type": error_type,
            "error_message": redact(str(error_message)[:500]),
            "wait_time_seconds": wait_time,
            "messages_length": messages_length
        }
        with self._lock:
            self.failures.append(failure)
            try:
                # 走 logging：单一文件流、自动轮转、单实例锁保护
                self._logger.info(json.dumps(failure, ensure_ascii=False))
            except Exception as e:
                logger.error(f"写入失败日志出错: {e}")

    def get_summary(self) -> dict:
        """获取失败统计摘要（线程安全）"""
        with self._lock:
            if not self.failures:
                return {"total_failures": 0}

            error_types = {}
            for f in self.failures:
                etype = f["error_type"]
                error_types[etype] = error_types.get(etype, 0) + 1

            return {
                "total_failures": len(self.failures),
                "error_types": error_types,
                "first_failure": self.failures[0]["timestamp"],
                "last_failure": self.failures[-1]["timestamp"]
            }

    def reset(self):
        """清空内存统计（队列模式下每本书开始前调用，避免跨书累积）"""
        with self._lock:
            self.failures.clear()


failure_logger = FailureLogger()


def _get_failure_logger():
    """获取 FailureLogger 单例（线程安全，内部已加锁）"""
    return failure_logger
