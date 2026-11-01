"""测试漏引号修复与 parse_json_robust 诊断信息"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.utils.json_utils import (
    repair_missing_quotes,
    parse_json_robust,
    _normalize_fullwidth_outside_strings,
)


def test_key_missing_opening_quote():
    """键漏开引号：{id":2 -> {"id":2（今天日志里的真实样本形态）"""
    broken = '{"core_events":[{"id":1,"event":"a"},{id":2,"event":"b"}],"ok":true}'
    data, detail = parse_json_robust(broken)
    assert data is not None
    assert data["core_events"][1]["id"] == 2
    assert data["core_events"][1]["event"] == "b"
    assert detail == "已自动修复JSON（漏引号）后解析成功"


def test_value_missing_opening_quote_ideographic_comma():
    """值漏开引号且以、开头：ast 会报 invalid character '、'(U+3001)"""
    broken = '{"a":1,"b":、伏笔线索"}'
    data, detail = parse_json_robust(broken)
    assert data is not None
    assert data["b"] == "、伏笔线索"
    assert detail == "已自动修复JSON（漏引号）后解析成功"


def test_value_missing_opening_quote_fullwidth_paren():
    """值漏开引号且以（开头：ast 会报 invalid character '（'(U+FF08)"""
    broken = '{"a":1,"b":（重要内容）"}'
    data, detail = parse_json_robust(broken)
    assert data is not None
    assert data["b"] == "（重要内容）"
    assert detail == "已自动修复JSON（漏引号）后解析成功"


def test_valid_json_not_touched():
    """合法 JSON 不被误伤（含嵌套、数组、Unicode、转义引号）"""
    text = '{"a":{"b":[1,2],"c":"x,y"},"d":"\\"esc\\"","e":"中文"}'
    assert repair_missing_quotes(text) == text
    data, detail = parse_json_robust(text)
    assert data is not None
    assert detail is None


def test_boolean_and_number_values_not_touched():
    """布尔/数字值不受值修复正则影响"""
    text = '{"a":true,"b":123,"c":[true,false],"d":null}'
    assert repair_missing_quotes(text) == text


def test_empty_text_detail():
    """空文本返回明确诊断"""
    data, detail = parse_json_robust("")
    assert data is None
    assert detail == "空文本"


def test_python_dict_style_still_works():
    """json5/ast 兼容能力不回退：单引号 Python 字典格式"""
    data, detail = parse_json_robust("{'a': 'x', 'b': 1}")
    assert data is not None
    assert data["a"] == "x"


# === 全角标点状态机归一 ===


class TestNormalizeFullwidth:
    """_normalize_fullwidth_outside_strings：只动 string 外部，string 内部保持原样"""

    def test_escaped_quote_inside_string_preserves_fullwidth(self):
        # \" 是合法转义，不应被当成 string 结束
        text = r'"a\"b"，"c":1'  # "a\"b"，"c":1
        result = _normalize_fullwidth_outside_strings(text)
        # \" 不切换 in_string，所以整段都在 string 内
        # 第一个 " 是开 string，最后的 " 才是闭 string
        # 那中间的 " 也都在 string 内
        # 整体是: "a\"b"，"c":1
        # 第一个 " (开), a, \, ", b, " (??), ， (这是string 内的), " (闭), , (string外,保持), " (开), c, " (闭), :1
        # 嗯这个case有点tricky
        # 实际上: "a\"b"，"c":1
        # 第一个 " 开启 string
        # a, \, ", b -> 这是a\"b, " 是转义的引号
        # 接下来 ",  切换 in_string = False
        # ，是 string 外的全角逗号，应被替换为 ,
        # " 切换回 in_string = True
        # c 是 string 内的字符
        # " 关闭 string
        # :1 是 string 外
        # 结果: "a\"b","c":1
        assert result == r'"a\"b","c":1'

    def test_fullwidth_comma_outside_string_replaced(self):
        # "value"，"key" 中间的全角逗号（string 外的）应被替换
        text = '"a"，"b"'
        assert _normalize_fullwidth_outside_strings(text) == '"a","b"'

    def test_fullwidth_colon_outside_string_replaced(self):
        # "key"：后面是 string 外的全角冒号
        text = '"key"：1'
        assert _normalize_fullwidth_outside_strings(text) == '"key":1'

    def test_fullwidth_paren_outside_string_replaced(self):
        text = '{"key"：（val）}'
        assert _normalize_fullwidth_outside_strings(text) == '{"key":(val)}'

    def test_fullwidth_inside_chinese_string_preserved(self):
        # "太子跳下凡间，袖子被云挂了一下" — 字符串内的全角逗号必须保留
        text = '"event": "太子跳下凡间，袖子被云挂了一下"'
        result = _normalize_fullwidth_outside_strings(text)
        assert "太子跳下凡间，袖子被云挂了一下" in result  # 内部全角保留

    def test_mixed_complex_real_case(self):
        # 真实 LLM 输出（minimaxi/M2.7）：结构标点全角，字符串内容全角
        text = (
            "{\n"
            '  "canonical_name": "宁安县"，\n'
            '  "aliases": ["宁安县城"]，\n'
            '  "description": "测试"：\n'
            '  "count": 10\n'
            "}"
        )
        result = _normalize_fullwidth_outside_strings(text)
        # 外部全角应被替换
        assert '宁安县",' in result  # 第一个 "value"， 变成 "value",
        assert '宁安县城"],' in result  # 第二个，
        assert '"description": "测试":' in result  # "： → :
        # 字符串内：description 的值 "测试" 保持
        assert '"description": "测试":' in result

    def test_empty_string(self):
        assert _normalize_fullwidth_outside_strings("") == ""

    def test_no_fullwidth_passes_through(self):
        text = '{"a":1,"b":"hello, world"}'
        assert _normalize_fullwidth_outside_strings(text) == text

    def test_array_with_fullwidth_commas(self):
        text = '["a"，"b"，"c"]'
        # 数组内的全角逗号（语法）应被替换
        # 元素内的字符保持原样（"a" "b" "c" 是单独的 string）
        assert _normalize_fullwidth_outside_strings(text) == '["a","b","c"]'

    def test_nested_strings(self):
        text = '"outer \"inner\" text"，"key":1'
        result = _normalize_fullwidth_outside_strings(text)
        assert '"key":1' in result
        assert "inner" in result  # 内部字符串内容保持


class TestParseJsonRobustWithFullwidth:
    """parse_json_robust 直接处理全角标点（策略0 归一化后，策略1 json.loads 成功）"""

    def test_fullwidth_comma_json_parses_directly(self):
        # 策略0 + 策略1 直接通过，无需 json_repair 兜底
        text = '{"canonical_name": "宁安县"，"type": "县城"}'
        data, detail = parse_json_robust(text)
        assert data is not None
        assert data["canonical_name"] == "宁安县"
        assert data["type"] == "县城"
        # detail 为 None 表示直接成功（没经过漏引号修复）
        assert detail is None

    def test_fullwidth_colon_in_object_parses_directly(self):
        text = '{"key"：1，"key2"：2}'
        data, detail = parse_json_robust(text)
        assert data is not None
        assert data["key"] == 1
        assert data["key2"] == 2

    def test_fullwidth_in_string_value_preserved(self):
        # 字符串内的全角逗号/冒号必须保留（中文文本渲染需要）
        text = '{"event": "太子跳下凡间，袖子被云挂了一下"}'
        data, detail = parse_json_robust(text)
        assert data is not None
        assert data["event"] == "太子跳下凡间，袖子被云挂了一下"

    def test_real_llm_output_pattern(self):
        # 模拟 minimaxi/M2.7 实际 LLM 输出（否则会卡 ast.literal_eval）
        text = (
            "{\n"
            '  "locations": [\n'
            '    {\n'
            '      "canonical_name": "宁安县"，\n'
            '      "aliases": ["宁安县城"]，\n'
            '      "parent": "京畿府"，\n'
            '      "type": "县城"，\n'
            '      "description": "谢怜故国"：\n'
            '      "chapter_count": 10\n'
            '    }\n'
            '  ]\n'
            "}"
        )
        data, detail = parse_json_robust(text)
        assert data is not None
        assert data["locations"][0]["canonical_name"] == "宁安县"
        assert data["locations"][0]["aliases"] == ["宁安县城"]
        assert data["locations"][0]["description"] == "谢怜故国"


# === 全策略 dict 守卫 + safe_save_json 唯一 tmp 名 ===


def test_parse_robust_never_returns_non_dict():
    """契约是仅返回 dict——顶层数组/字符串必须判失败而不是带病返回"""
    for bad in ('[1,2,3]', '"hello"', "['a','b']", 'null', '123'):
        result, err = parse_json_robust(bad)
        assert result is None, f"{bad!r} 不应返回 {result!r}"

def test_strategy8_single_quoted_array_rejected():
    """单引号数组不得被修复成功后当 dict 返回"""
    result, _ = parse_json_robust("['a','b']")
    assert result is None


def test_safe_save_json_unique_tmp_concurrent(tmp_path):
    """固定 .tmp 名并发写同一目标会交错/PermissionError；唯一名下应全部成功"""
    import threading
    from backend.utils.json_utils import safe_save_json
    target = tmp_path / "t.json"
    errors = []

    def worker(i):
        if not safe_save_json({"i": i}, target):
            errors.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert errors == []
    import json as _json
    data = _json.loads(target.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"i"} and isinstance(data["i"], int)
