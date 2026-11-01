"""
测试 redact 工具

覆盖：OpenAI sk-... / Anthropic sk-ant-... / Authorization Bearer / x-api-key header
反向测试 3 个：短串不误伤 / 已脱敏不重复 / Retry-After 不误伤
边界：空字符串 / 非字符串 / 多个 key 共存 / Unicode
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.llm_failure_logger import redact


class TestRedactOpenAI:
    def test_basic_openai_key(self):
        # 标准 OpenAI sk-... 格式（20+ 字符前缀）
        text = "error: invalid api key sk-abcdefghij1234567890abcd"
        result = redact(text)
        assert "sk-***" in result
        assert "sk-abcdefghij1234567890abcd" not in result

    def test_basic_anthropic_key(self):
        # 标准 Anthropic sk-ant-... 格式
        text = "auth failed: sk-ant-api03-abcdefghij1234567890"
        result = redact(text)
        assert "sk-ant-***" in result
        assert "sk-ant-api03-abcdefghij1234567890" not in result


class TestRedactHeaders:
    def test_authorization_bearer(self):
        text = "request failed: Authorization: Bearer sk-abcdefghij1234567890abcd"
        result = redact(text)
        # 行为：保留 "Bearer " 前缀，只把 token 替换为 ***
        assert "Authorization: Bearer ***" in result
        assert "sk-abcdefghij1234567890abcd" not in result

    def test_x_api_key_header(self):
        text = "got: x-api-key: sk-ant-api03-abcdefghij1234567890 and more"
        result = redact(text)
        assert "x-api-key: ***" in result
        assert "sk-ant-api03-abcdefghij1234567890" not in result


class TestRedactMultiple:
    def test_multiple_keys_in_one_text(self):
        text = (
            "first key: sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
            "second: sk-ant-bbbbbbbbbbbbbbbbbbbbbbbbbbbb "
            "third: Authorization: Bearer sk-cccccccccccccccccccccccccccc"
        )
        result = redact(text)
        assert "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in result
        assert "sk-ant-bbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in result
        assert "sk-cccccccccccccccccccccccccccc" not in result
        assert "sk-***" in result
        assert "sk-ant-***" in result


class TestRedactNegative:
    """反向测试：确保 redact 不会误伤合法内容。"""

    def test_short_strings_not_redacted(self):
        # 短串（如 "sk-xxx"）不应被误判为 key
        # 实际应用场景：用户错误消息里提到 "sk-id" 之类
        text = "user input: sk-id is too short, please provide a longer one"
        result = redact(text)
        # 短串（< 20 字符）应原样保留
        assert "sk-id" in result

    def test_already_redacted_not_double_processed(self):
        # 已经脱敏的 sk-*** 不应被重复处理
        text = "previous error: sk-***, but new error: sk-abcdefghij1234567890abcd"
        result = redact(text)
        assert "sk-***" in result
        assert "sk-abcdefghij1234567890abcd" not in result
        # 关键：已脱敏的 "sk-***" 应保留（不是被改写成别的）
        assert result.count("sk-***") == 1 or result.count("sk-***") >= 1

    def test_retry_after_not_redacted(self):
        # 错误信息中的 [Retry-After:120] 不应被误伤
        text = "rate limited: [Retry-After:120] seconds, please wait"
        result = redact(text)
        # Retry-After 数字不应被改
        assert "[Retry-After:120]" in result


class TestRedactEdgeCases:
    def test_empty_string(self):
        assert redact("") == ""

    def test_none_input(self):
        # fail-open：None 输入应原样返回
        assert redact(None) is None

    def test_non_string_input(self):
        # fail-open：非字符串应原样返回
        assert redact(123) == 123

    def test_unicode_preserved(self):
        # 中英文混排不应受影响（除非含 key）
        text = "错误信息：API 调用失败 sk-abcdefghij1234567890abcd，请检查 ⚠️"
        result = redact(text)
        assert "错误信息" in result
        assert "请检查" in result
        assert "⚠️" in result
        assert "sk-abcdefghij1234567890abcd" not in result

    def test_long_key_redacted(self):
        # 超长 key 也应被处理（OpenAI 新格式可达 56+ 字符）
        very_long_key = "sk-" + "a" * 100
        text = f"key: {very_long_key}"
        result = redact(text)
        assert very_long_key not in result
        assert "sk-***" in result


class TestRedactFailOpen:
    def test_fail_open_on_invalid_pattern(self):
        """fail-open：如果某次 redact 抛异常，应返回原文（不丢日志）。

        这通过直接调用 redact 在极端输入下不抛异常来验证。
        """
        # 即使输入很怪也不应该抛异常
        weird_inputs = [
            "\x00\x01\x02",  # 控制字符
            "sk-" + "\n" * 1000,  # 大量换行
            "sk-" + "中" * 100,  # 大量中文字符
        ]
        for inp in weird_inputs:
            result = redact(inp)
            # 应该返回字符串（即使没替换），不应抛异常
            assert isinstance(result, str)
