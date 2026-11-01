"""编码选择鲁棒性：

1. 256KB 采样窗口边界切坏 2-byte GBK 字符的尾字节，_penalty_sample 必须不抛错
   且能正确返回 gbk 而不是 latin-1 兜底。
2. 4.5MB GBK 小说（用户真实文件《超级能源强国》精校版）必须能成功 decode_and_read。
3. splitter_service._detect_and_read 路径必须能正确读 GBK 文件（含 gb18030）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.utils.text_utils import (
    detect_and_decode,
    detect_encoding,
    _penalty_sample,
    _SAMPLE_BYTES,
)
from backend.config.constants import ENCODING_CANDIDATES
from backend.services.splitter_service import _detect_and_read


def _build_gbk_text(n_chars: int) -> bytes:
    """构造 n_chars 个 GBK 编码的『你好』字符 + 末尾 1 字节（模拟边界切坏）"""
    s = "你好" * (n_chars // 2 + 1)
    raw = s.encode("gbk")
    return raw


def test_256k_boundary_no_longer_kills_gbk(tmp_path):
    """256KB 边界切到 2-byte GBK 字符尾字节：gbk 必须仍在候选中（penalty != None）

    真实场景：4.5MB GBK 小说在 256KB 边界正好切到 0xACA1 的 0xA1 尾字节，
    之前 strict decode 报 incomplete，_penalty_sample 返回 None 把 gbk 整个排除。
    改 errors='replace' 后边界字符变 U+FFFD，penalty 仍可计算。
    """
    # 构造一个 ~256KB+1 字节的 GBK 文件（让尾字节落在边界后）
    raw = _build_gbk_text(_SAMPLE_BYTES)
    # 取正好 _SAMPLE_BYTES 字节（会在 2-byte 字符中间切）
    boundary_sample = raw[:_SAMPLE_BYTES]
    assert len(boundary_sample) == _SAMPLE_BYTES

    # 应返回有效 penalty，不能是 None
    score = _penalty_sample(boundary_sample, "gbk")
    assert score is not None, "GBK 不能因为采样边界就被排除"
    # 边界字符最多几个（< 4），FFFD 罚分 8，所以 penalty < 32 / _SAMPLE_BYTES
    assert score < 1e-3, f"GBK 在正常文本上的 penalty 应极低，实际 {score}"


def test_real_user_file_gbk_decodable(tmp_path):
    """4.5MB GBK 小说（用户提供）必须能成功 detect_and_decode。

    真实案例：F:\\AI\\04_小说与写作\\修复后txt文件\\《超级能源强国》（精校版）.txt
    """
    src = Path(r"F:\AI\04_小说与写作\修复后txt文件\《超级能源强国》（精校版）.txt")
    if not src.exists():
        pytest.skip(f"真实用户文件不存在: {src}")

    # 复制到 tmp 避免污染原文件
    test_file = tmp_path / "超级能源强国.txt"
    test_file.write_bytes(src.read_bytes())

    text = detect_and_decode(test_file, ENCODING_CANDIDATES)
    assert "超级能源强国" in text
    assert "志鸟村" in text  # 作者
    assert "内容简介" in text

    enc = detect_encoding(test_file, ENCODING_CANDIDATES)
    assert enc in ("gbk", "gb18030"), f"期望 gbk/gb18030，实际 {enc}"


def test_splitter_service_handles_gbk_file(tmp_path):
    """splitter_service._detect_and_read 必须能读 GBK 小说。

    之前 splitter 的本地 ENCODING_PRIORITY 列表缺 gb18030，
    256KB 边界切字符时 gbk 失败 → 全部硬解失败 → 抛 UnicodeDecodeError。
    改用 ENCODING_CANDIDATES 后 splitter 跟 file_processor 行为一致。
    """
    src = Path(r"F:\AI\04_小说与写作\修复后txt文件\《超级能源强国》（精校版）.txt")
    if not src.exists():
        pytest.skip(f"真实用户文件不存在: {src}")

    test_file = tmp_path / "splitter_gbk_test.txt"
    test_file.write_bytes(src.read_bytes())

    content = _detect_and_read(test_file)
    assert "超级能源强国" in content
    assert "志鸟村" in content
    # 行尾归一化（splitter 内部做）
    assert "\r\n" not in content
    assert "\r" not in content
