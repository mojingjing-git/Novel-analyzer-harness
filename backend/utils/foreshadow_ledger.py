"""
伏笔账本模块
管理伏笔的生命周期：新增、回收、休眠判定
"""

import logging
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime

from ..utils.json_utils import safe_save_json, safe_load_json

logger = logging.getLogger(__name__)

# Schema 版本号
# v1: 初始版本
# v2: ForeshadowItem 新增 needs_review 可选字段（向后兼容，旧 v1 JSON 仍可加载）
# v3: ForeshadowItem 新增 composite_score + importance（向后兼容，旧 v2 JSON 仍可加载）
SCHEMA_VERSION = 3

# 休眠阈值
# DORMANT_BATCH_THRESHOLD 已废弃（2026-08-13）：批次阈值相对批次数（10-20 批）不可达，
# 且 last_seen_batch 在 reconciliation 中的语义不可靠，无数据支撑——休眠改以章距为主判据。
DORMANT_CHAPTER_THRESHOLD = 200   # 章节跨度阈值（最后出现后 N 章未见即休眠）

# 置信度字符串→数值映射（LLM输出"高"/"中"/"低"时使用）
_CONFIDENCE_MAP = {"高": 0.9, "中": 0.6, "低": 0.3}


@dataclass
class ForeshadowItem:
    """单个伏笔条目"""
    id: str                                # 伏笔唯一ID
    description: str                       # 伏笔描述
    first_seen_chapter: int                # 首次出现章节
    first_seen_batch: int                  # 首次出现批次
    last_seen_chapter: int                 # 最后出现章节
    last_seen_batch: int                   # 最后出现批次
    status: str = "active"                 # active/resolved/dormant
    resolution: Optional[str] = None       # 回收方式描述
    resolved_chapter: Optional[int] = None # 回收章节
    resolved_batch: Optional[int] = None   # 回收批次
    source_type: str = "llm_judged"        # llm_judged/rule_detected/human_override/auto_detected
    evidence_chapters: List[int] = field(default_factory=list)  # 证据章节列表
    confidence: float = 0.8                # 置信度 0-1
    notes: List[str] = field(default_factory=list)  # 备注
    needs_review: bool = False             # 是否需要人工复核（低置信度 / 存疑时标记）
    composite_score: int = 0               # 组合因子分数（imp×100 + conf×10 + evi≤50 + span≤49）
    importance: str = "中"                 # 重要度（高/中/低，从 catalog 同步）

    @classmethod
    def from_dict(cls, data: dict) -> 'ForeshadowItem':
        """从字典创建（白名单过滤，防御 JSON 中的多余字段导致 TypeError）"""
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class ForeshadowLedger:
    """伏笔账本"""
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    batch_size_snapshot: int = 0            # 当时的批次大小快照
    total_chapters: int = 0                 # 总章节数
    items: List[ForeshadowItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为字典"""
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'ForeshadowLedger':
        """从字典创建账本"""
        items = [ForeshadowItem.from_dict(item) for item in data.get('items', [])]
        return cls(
            schema_version=data.get('schema_version', SCHEMA_VERSION),
            created_at=data.get('created_at', datetime.now().isoformat()),
            updated_at=data.get('updated_at', datetime.now().isoformat()),
            batch_size_snapshot=data.get('batch_size_snapshot', 0),
            total_chapters=data.get('total_chapters', 0),
            items=items
        )

    def save(self, path: Path) -> bool:
        """保存账本到文件"""
        try:
            self.updated_at = datetime.now().isoformat()
            return safe_save_json(self.to_dict(), path)
        except Exception as e:
            logger.error(f"保存伏笔账本失败: {e}")
            return False

    @classmethod
    def load(cls, path: Path) -> 'ForeshadowLedger':
        """从文件加载账本"""
        data = safe_load_json(path)
        if data is None:
            return cls()
        return cls.from_dict(data)

    def reconcile_and_update(self, batch_idx: int, current_chapter: Optional[int] = None) -> None:
        """
        批次完成后执行 reconciliation：
        1. 检查休眠伏笔（章节跨度按"当前处理进度"计算，见 current_chapter 参数）
        2. 更新状态

        休眠判定（2026-08-13 重构，BUG-B）：
        - 主判据 = 章距：last_seen_chapter（最后出现的真实章节，由 catalog 证据章维护）
          距当前进度 ≥ DORMANT_CHAPTER_THRESHOLD(200) 即休眠
        - 删除批次条件：旧实现 DORMANT_BATCH_THRESHOLD=15 相对实际批次数
          （总块数/batch_size，通常 10-20 批）永远不可达；且 last_seen_batch 语义
          已被 BUG-A/BUG-C 证实不可靠（未回收即推进批号、跨运行批次号混用），无数据支撑。

        2026-08-07 修复（P0-2）：原实现用 self.total_chapters（全书最终章数）
        计算"多少章未见"，长书会提前把后埋的伏笔判为休眠；应传当前批的 ch_end。
        current_chapter 为 None 时回退旧行为（total_chapters），兼容历史调用方。
        """
        for item in self.items:
            if item.status != 'active':
                continue

            if current_chapter is not None:
                chapter_span = (current_chapter - item.last_seen_chapter) >= DORMANT_CHAPTER_THRESHOLD
            else:
                chapter_span = (self.total_chapters - item.last_seen_chapter) >= DORMANT_CHAPTER_THRESHOLD

            if chapter_span:
                item.status = 'dormant'
                item.notes.append(f"批次{batch_idx}时自动标记为休眠（{DORMANT_CHAPTER_THRESHOLD}章未见）")

    def audit_text_for_report(self) -> str:
        """
        生成审计报告文本
        """
        active = [i for i in self.items if i.status == 'active']
        resolved = [i for i in self.items if i.status == 'resolved']
        dormant = [i for i in self.items if i.status == 'dormant']

        lines = ["## 伏笔审计报告\n"]
        lines.append(f"**总计**: {len(self.items)} 个伏笔")
        lines.append(f"- 活跃: {len(active)}")
        lines.append(f"- 已回收: {len(resolved)}")
        lines.append(f"- 休眠: {len(dormant)}\n")

        if resolved:
            lines.append("### 已回收伏笔")
            for item in resolved:
                lines.append(f"- **{item.id}**: {item.description}")
                lines.append(f"  - 回收方式: {item.resolution}")
                lines.append(f"  - 回收章节: 第{item.resolved_chapter}章")
                lines.append(f"  - 来源类型: {item.source_type}")
                if item.confidence < 0.8:
                    lines.append(f"  - 置信度: {item.confidence}（需人工复核）")

        if active:
            lines.append("\n### 活跃伏笔")
            for item in active:
                lines.append(f"- **{item.id}**: {item.description}")
                lines.append(f"  - 首次出现: 第{item.first_seen_chapter}章")
                lines.append(f"  - 最近出现: 第{item.last_seen_chapter}章")
                lines.append(f"  - 证据章节: {item.evidence_chapters}")

        if dormant:
            lines.append("\n### 休眠伏笔（长期未出现）")
            for item in dormant:
                lines.append(f"- **{item.id}**: {item.description}")
                lines.append(f"  - 首次出现: 第{item.first_seen_chapter}章")
                lines.append(f"  - 最近出现: 第{item.last_seen_chapter}章")
                lines.append(f"  - 休眠原因: {item.notes[-1] if item.notes else '未知'}")

        return '\n'.join(lines)

    def _find_item(self, fs_id: str) -> Optional[ForeshadowItem]:
        """查找伏笔条目"""
        for item in self.items:
            if item.id == fs_id:
                return item
        return None

    def get_item_count(self) -> Dict[str, int]:
        """获取各状态伏笔数量"""
        return {
            'total': len(self.items),
            'active': sum(1 for i in self.items if i.status == 'active'),
            'resolved': sum(1 for i in self.items if i.status == 'resolved'),
            'dormant': sum(1 for i in self.items if i.status == 'dormant')
        }
