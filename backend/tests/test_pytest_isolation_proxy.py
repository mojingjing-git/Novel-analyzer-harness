"""S0 回归：测试进程代理环境净化（PLAN v2 §S0-4、REPORT-review C-17）。

历史事故：本机 ``NO_PROXY`` 含方括号写法 ``[::1]``，httpx 把它当 URL 解析，
端口段 ``:1]`` 转 int 失败 → ``httpx.InvalidURL: Invalid port: ':1]'``，
在 ``AsyncOpenAI(...)`` 构造期抛异常，导致 74 个用例整片假失败（实测清掉后
864 全绿）。净化必须发生在任何 ``backend.*`` import 之前，由
``pytest_isolation.install()`` 承担，本文件只锁行为。
"""

from __future__ import annotations

import os

import pytest

import pytest_isolation


class TestNormalizeNoProxy:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("localhost,127.0.0.1,::1,[::1]", "localhost,127.0.0.1,::1,::1"),
            ("localhost,127.0.0.1,::1", "localhost,127.0.0.1,::1"),
            ("[::1]", "::1"),
            ("  [::1] , [fe80::1]  ", "::1,fe80::1"),
            ("", ""),
            ("localhost,,127.0.0.1", "localhost,127.0.0.1"),
            ("*", "*"),
        ],
    )
    def test_normalizes_bracketed_ipv6(self, raw, expected):
        assert pytest_isolation.normalize_no_proxy(raw) == expected

    def test_idempotent(self):
        once = pytest_isolation.normalize_no_proxy("a,[::1]")
        assert pytest_isolation.normalize_no_proxy(once) == once


class TestProxyUrlParseable:
    @pytest.mark.parametrize(
        "url,ok",
        [
            ("http://127.0.0.1:7897", True),
            ("http://127.0.0.1", True),
            ("socks5h://127.0.0.1:1080", True),
            ("http://[::1]:7897", True),
            ("http://user:pass@127.0.0.1:7897", True),
            ("http://127.0.0.1:abc", False),
            ("http://127.0.0.1:", False),
        ],
    )
    def test_port_must_be_numeric(self, url, ok):
        assert pytest_isolation._proxy_url_is_parseable(url) is ok


class TestSanitizeProxyEnv:
    def test_rewrites_brackets_and_records_change(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
        changes = pytest_isolation.sanitize_proxy_env()
        assert os.environ["NO_PROXY"] == "localhost,::1"
        assert "NO_PROXY" in changes
        assert pytest_isolation.PROXY_SANITIZATION["NO_PROXY"].endswith("localhost,::1")

    def test_drops_unparseable_proxy_url(self, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:abc")
        changes = pytest_isolation.sanitize_proxy_env()
        assert "HTTP_PROXY" not in os.environ
        assert "unparseable" in changes["HTTP_PROXY"]

    def test_keeps_valid_proxy_untouched(self, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setenv("NO_PROXY", "localhost,::1")
        changes = pytest_isolation.sanitize_proxy_env()
        assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7897"
        assert "HTTP_PROXY" not in changes
        assert "NO_PROXY" not in changes


class TestInstalledProcessIsSane:
    def test_install_already_sanitized_this_process(self):
        # conftest 在 import 期已调用 install()；此处只验证结果
        pytest_isolation.assert_proxy_env_sane()

    def test_no_bracket_remains_in_no_proxy(self):
        for key in ("NO_PROXY", "no_proxy"):
            value = os.environ.get(key)
            if value is None:
                continue
            assert "[" not in value, f"{key} 仍含方括号：{value!r}"

    def test_client_construction_does_not_raise(self):
        """症状级回归：构造 AsyncOpenAI/AsyncAnthropic 不得抛 InvalidURL。"""
        openai = pytest.importorskip("openai")
        anthropic = pytest.importorskip("anthropic")

        openai.AsyncOpenAI(base_url="https://example.invalid/v1", api_key="test-key")
        anthropic.AsyncAnthropic(base_url="https://example.invalid", api_key="test-key")
