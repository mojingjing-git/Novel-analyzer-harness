"""代理环境变量净化工具（纯标准库，无上层依赖）。

将进程内会让 httpx 构造期抛 InvalidURL 的代理写法（如 NO_PROXY 里的方括号
IPv6 字面量 ``[::1]`` 或不可解析端口）规范化，供测试隔离与运维脚本共用。
"""

from __future__ import annotations

import os
import re

PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)

PROXY_SANITIZATION: dict[str, str] = {}

_BRACKETED_HOST = re.compile(r"^\[(?P<host>.+)\]$")
_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://")
_DIGITS_RE = re.compile(r"^\d+$")


def normalize_no_proxy(value: str) -> str:
    """把 ``no_proxy``/``NO_PROXY`` 里的方括号 IPv6 改写成等价裸写法。"""
    tokens: list[str] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        match = _BRACKETED_HOST.match(token)
        if match:
            token = match.group("host")
        tokens.append(token)
    return ",".join(tokens)


def _proxy_url_is_parseable(value: str) -> bool:
    """代理 URL 的端口段必须为纯数字，否则 httpx 构造期会抛 InvalidURL。"""
    match = _SCHEME_RE.match(value.strip())
    if not match:
        return True
    rest = value.strip()[match.end():]
    hostport = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    if hostport.startswith("["):
        closing = hostport.find("]")
        if closing < 0:
            return False
        tail = hostport[closing + 1:]
        return not tail or (tail.startswith(":") and _DIGITS_RE.match(tail[1:]) is not None)
    if ":" in hostport:
        port = hostport.rsplit(":", 1)[1]
        return bool(_DIGITS_RE.match(port))
    return True


def sanitize_proxy_env() -> dict[str, str]:
    """在当前进程内修正会让 HTTP 客户端构造期抛异常的代理写法。"""
    changes: dict[str, str] = {}
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value is None:
            continue
        fixed = normalize_no_proxy(value) if "no_proxy" in key.lower() else value.strip()
        if "no_proxy" not in key.lower() and not _proxy_url_is_parseable(fixed):
            del os.environ[key]
            changes[key] = f"{value} -> <dropped:unparseable>"
            continue
        if fixed != value:
            os.environ[key] = fixed
            changes[key] = f"{value} -> {fixed}"
    PROXY_SANITIZATION.clear()
    PROXY_SANITIZATION.update(changes)
    return changes


def assert_proxy_env_sane() -> None:
    """确认当前进程内已无会让 httpx 抛 InvalidURL 的代理写法。"""
    problems: list[str] = []
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value is None:
            continue
        if "no_proxy" in key.lower():
            if value != normalize_no_proxy(value):
                problems.append(f"{key}={value!r} 仍含方括号 IPv6")
        elif not _proxy_url_is_parseable(value):
            problems.append(f"{key}={value!r} 端口段不可解析")
    if problems:
        raise RuntimeError(
            "进程代理环境未净化，httpx 构造期会抛 InvalidURL：" + "; ".join(problems)
        )
