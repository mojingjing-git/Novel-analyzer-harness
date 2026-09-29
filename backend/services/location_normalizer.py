"""
地点与空间关系归一化（2026-08-21 spec）

Phase 0 在最终总结之前跑：
- 0a-batches: 并发 LLM 归一化 locations
- 0b-batches: 并发 LLM 结构化 spatial_relationships
- 0b-dedupe: 机械去重同 (from, to) 对
"""
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from backend.utils.text_utils import is_same_location
from backend.utils.json_utils import safe_parse_json, extract_json_from_text

logger = logging.getLogger(__name__)


def _parse_llm_dict(llm_response: str) -> Optional[Dict[str, Any]]:
    """双路径解析 LLM 响应：先直接解析，失败则剥 markdown/前缀再解析

    解决 minimaxi/M2.7 等 LLM 偶尔在 JSON 前后加 markdown ```json``` 块或中文前缀
    （"好的，以下是结果："）的问题。final_summary 已用同样的双路径策略。
    """
    parsed = safe_parse_json(llm_response)
    if isinstance(parsed, dict):
        return parsed
    extracted = extract_json_from_text(llm_response)
    if extracted:
        parsed = safe_parse_json(extracted)
        if isinstance(parsed, dict):
            return parsed
    return None


def aggregate_locations(raw_locations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """预聚合：字面同名 + 相似名（SequenceMatcher ≥ 0.85）→ group

    每个 group 包含 aliases（字面名列表）、types（Counter）、parents（Counter）、
    chapter_count、sample_desc（任一非空描述）。
    按 chapter_count 降序返回。
    """
    name_meta: Dict[str, Dict[str, Any]] = {}
    for loc in raw_locations:
        n = (loc.get("name") or "").strip()
        if not n:
            continue
        if n not in name_meta:
            name_meta[n] = {
                "types": Counter(),
                "parents": Counter(),
                "descs": set(),
                "chapters": set(),
            }
        m = name_meta[n]
        t = (loc.get("type") or "").strip()
        m["types"][t] += 1
        p = (loc.get("parent") or "").strip()
        if p:
            m["parents"][p] += 1
        d = (loc.get("description") or "").strip()
        if d:
            m["descs"].add(d)
        if "chapter" in loc and loc["chapter"] is not None:
            m["chapters"].add(loc["chapter"])

    # Union-Find
    parent_uf = {n: n for n in name_meta}

    def _find(x):
        while parent_uf[x] != x:
            parent_uf[x] = parent_uf[parent_uf[x]]
            x = parent_uf[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent_uf[ra] = rb

    names = list(name_meta.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if is_same_location(names[i], names[j]):
                _union(names[i], names[j])

    groups_dict: Dict[str, List[str]] = {}
    for n in names:
        groups_dict.setdefault(_find(n), []).append(n)

    result: List[Dict[str, Any]] = []
    for aliases in groups_dict.values():
        agg_types, agg_parents, agg_descs, agg_chapters = Counter(), Counter(), set(), set()
        for a in aliases:
            m = name_meta[a]
            agg_types.update(m["types"])
            agg_parents.update(m["parents"])
            agg_descs |= m["descs"]
            agg_chapters |= m["chapters"]
        result.append({
            "aliases": sorted(aliases, key=len),
            "types": dict(agg_types),
            "parents": dict(agg_parents),
            "chapter_count": len(agg_chapters),
            "sample_desc": next(iter(agg_descs), ""),
        })

    result.sort(key=lambda g: -g["chapter_count"])
    return result


def aggregate_spatial_pairs(raw_rels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """预聚合 spatial_relationships：相同 (from, to) 对的多条记录合并为 1 条

    返回按 chapters 数量降序的 pair 列表，每个 dict 包含 from/to/relations(chapters)/chapters(list)。
    """
    pair_meta: Dict[tuple, Dict[str, Any]] = {}
    for rel in raw_rels:
        f = (rel.get("from") or "").strip()
        t = (rel.get("to") or "").strip()
        if not f or not t:
            continue
        key = (f, t)
        if key not in pair_meta:
            pair_meta[key] = {"relations": set(), "chapters": set()}
        rel_text = (rel.get("relation") or "").strip()
        pair_meta[key]["relations"].add(rel_text)
        if "chapter" in rel and rel["chapter"] is not None:
            pair_meta[key]["chapters"].add(rel["chapter"])

    pairs: List[Dict[str, Any]] = [
        {
            "from": k[0],
            "to": k[1],
            "relations": sorted(v["relations"]),
            "chapters": sorted(v["chapters"]),
        }
        for k, v in pair_meta.items()
    ]
    pairs.sort(key=lambda p: -len(p["chapters"]))
    return pairs


_LOCATION_SYSTEM_PROMPT = """你是中文网络小说数据归一化专家。你的任务是把同一地点在不同章节里的不同写法/不同分类合并为规范条目。

## 任务 1 — 地点归一化（locations）

输入是一组预聚合的地点，每个 group 包含：
- 该组里出现过的所有原始地名（aliases）
- 这些地点的所有 type 变体 + 各自次数
- 这些地点的所有 parent 变体 + 各自次数
- 出现总章节数
- 1 个样本描述（其他描述类似）

对每个 group，你需要输出：
{
  "canonical_name": "宁安县",        // 字面等于 aliases 中的某一个
  "aliases": ["宁安县", "宁安县城", "宁安县桐树坊"],   // 合并原始所有名称
  "parent": "京畿府",                // 最合理的上级；必须是某个原始 parent，否则 ""
  "type": "县城",                    // 选最合适的类型；可基于已有 type 创建新类
  "description": "大贞王朝下辖县城，..."   // 取最完整的描述
}

## 硬约束（违反则视为输出错误）

1. canonical_name 必须字面等于 aliases 中的某一个字符串。不容许改字、删字、合并字。
2. aliases 必须包含输入时该 group 的所有原始名称（字面保留，不得遗漏或改写）。
3. 输出必须是合法 JSON，locations 数组可为空。
"""


def build_location_prompt(
    groups: List[Dict[str, Any]],
    batch_idx: int,
    total_batches: int,
) -> List[Dict[str, str]]:
    """构造 locations 归一化的 system + user 消息"""
    user_lines = [f"## 待归一化的地点（batch {batch_idx}/{total_batches}，共 {len(groups)} 个 group）", ""]
    for i, g in enumerate(groups, 1):
        aliases_str = ", ".join(g["aliases"])
        types_str = ", ".join(f"{k}:{v}" for k, v in g["types"].items())
        parents_str = ", ".join(f"{k}:{v}" for k, v in g["parents"].items())
        user_lines.append(
            f"[{i}] aliases=[{aliases_str}]\n"
            f"    types={{{types_str}}}\n"
            f"    parents={{{parents_str}}}\n"
            f"    chapter_count={g['chapter_count']}\n"
            f"    sample_desc=\"{g['sample_desc']}\""
        )
    user_lines.append("")
    user_lines.append("请按 system prompt 规则输出 JSON {\"locations\": [...]}")
    return [
        {"role": "system", "content": _LOCATION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def parse_and_validate_locations(
    llm_response: str,
    batch: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """解析 LLM 输出并校验：canonical_name 必须字面等于某个 alias

    返回清洗后的 canonical 列表。无效条目被丢弃。
    """
    parsed = _parse_llm_dict(llm_response)
    if not parsed or not isinstance(parsed, dict):
        return []
    raw_locations = parsed.get("locations", [])
    if not isinstance(raw_locations, list):
        return []

    valid: List[Dict[str, Any]] = []
    for loc in raw_locations:
        if not isinstance(loc, dict):
            continue
        canonical = loc.get("canonical_name", "")
        aliases = loc.get("aliases", [])
        if not isinstance(canonical, str) or not isinstance(aliases, list):
            continue
        aliases = [a for a in aliases if isinstance(a, str)]
        # 硬约束1：canonical 必须字面等于某个 alias
        if canonical not in aliases:
            logger.warning(f"丢弃幻觉 location：canonical='{canonical}' 不在 aliases={aliases}")
            continue
        # 必填字段兜底
        valid.append({
            "canonical_name": canonical,
            "aliases": sorted(set(aliases)),
            "parent": loc.get("parent", "") or "",
            "type": loc.get("type", "") or "",
            "description": loc.get("description", "") or "",
        })
    return valid


_SPATIAL_SYSTEM_PROMPT = """你是中文小说空间关系结构化专家。你的任务是把每条自由文本空间关系描述抽取为结构化字段。

## 任务 — 空间关系结构化（spatial_relationships）

输入是一组预聚合的 (from, to) 关系对，每对包含：
- 原始 from / to 地名（已与地点归一化的白名单一致）
- 多条自由文本 relation 描述
- 出现的章节列表

对每个 pair，你需要输出：
{
  "from": "宁安县",                  // 必须引用白名单中的 canonical_name
  "to": "德胜府",                    // 同上
  "direction": "东南",              // 原文中的方向；无明确则 ""
  "distance_text": "约两三百里",    // 原文距离描述；无明确则 ""
  "distance_estimate_km": 130,      // 估算公里数（整数）；无明确数字则 null
  "relation_type": "相邻",          // 包含/相邻/接壤/隔海/跨越/途经/...；无法判断则 ""
  "evidence_chapters": [42, 43]     // 必须引用原始章节号数组（非空）
}

## 硬约束（违反则视为输出错误）

1. from / to 必须字面等于用户输入"白名单"中的某个 canonical_name，不容许创造新地名。
2. evidence_chapters 必须是非空整数数组。
3. 输出必须是合法 JSON，relationships 数组可为空。
"""


def build_spatial_prompt(
    pairs: List[Dict[str, Any]],
    whitelist: List[Dict[str, Any]],
    batch_idx: int,
    total_batches: int,
) -> List[Dict[str, str]]:
    """构造 spatial 结构化的 system + user 消息"""
    user_lines = [
        f"## 可用地名白名单（{len(whitelist)} 个）",
        "",
    ]
    for i, w in enumerate(whitelist, 1):
        aliases_str = ", ".join(w["aliases"])
        user_lines.append(f"[L{i}] canonical={w['canonical']}, aliases=[{aliases_str}]")
    user_lines.append("")
    user_lines.append(
        f"## 待结构化的空间关系（batch {batch_idx}/{total_batches}，共 {len(pairs)} 个 pair）"
    )
    user_lines.append("")
    for i, p in enumerate(pairs, 1):
        relations_str = "; ".join(f"\"{r}\"" for r in p["relations"])
        chapters_str = ", ".join(str(c) for c in p["chapters"])
        user_lines.append(
            f"[P{i}] from={p['from']}, to={p['to']}\n"
            f"    relations=[{relations_str}]\n"
            f"    chapters=[{chapters_str}]"
        )
    user_lines.append("")
    user_lines.append("请按 system prompt 规则输出 JSON {\"relationships\": [...]}")
    return [
        {"role": "system", "content": _SPATIAL_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def parse_and_validate_spatial(
    llm_response: str,
    whitelist_names: set,
) -> List[Dict[str, Any]]:
    """解析 LLM 输出并校验：from/to 必须字面在白名单中"""
    parsed = _parse_llm_dict(llm_response)
    if not parsed or not isinstance(parsed, dict):
        return []
    raw_rels = parsed.get("relationships", [])
    if not isinstance(raw_rels, list):
        return []

    valid: List[Dict[str, Any]] = []
    for rel in raw_rels:
        if not isinstance(rel, dict):
            continue
        f, t = rel.get("from", ""), rel.get("to", "")
        if f not in whitelist_names or t not in whitelist_names:
            logger.warning(f"丢弃幻觉 spatial：from='{f}' to='{t}'（不在白名单）")
            continue
        chapters = rel.get("evidence_chapters", [])
        if not isinstance(chapters, list) or not chapters:
            continue
        chapters_clean = [int(c) for c in chapters if isinstance(c, (int, float))]
        if not chapters_clean:
            continue
        valid.append({
            "from": f,
            "to": t,
            "direction": rel.get("direction", "") or "",
            "distance_text": rel.get("distance_text", "") or "",
            "distance_estimate_km": rel.get("distance_estimate_km"),
            "relation_type": rel.get("relation_type", "") or "",
            "evidence_chapters": sorted(set(chapters_clean)),
        })
    return valid


# ---------------------------------------------------------------------------
# Phase 0 orchestration（Task 6）
# ---------------------------------------------------------------------------
import asyncio
import hashlib
from datetime import datetime
from typing import Set

from backend.core.llm_client import LLMClient
from backend.utils.json_utils import safe_load_json, safe_save_json


_LOCATIONS_NORMALIZED_FILENAME = "locations_normalized.json"
_SPATIAL_NORMALIZED_FILENAME = "spatial_relationships_normalized.json"
# Phase 0a locations batch 控制（2026-08-22 调整：35K → 18K）：
# - 硬上限：单批 user prompt ≤ 18K chars（防单批 stall / 撞 timeout）
# - 实测《韩娱》512 group、user prompt ~70K chars → 切 ~5-6 batch 并发，
#   充分利用 config.analysis.concurrency=9；单批 stall 仅损失 ~17% group 而非 ~33%
_LOCATION_PROMPT_BUDGET_CHARS = 18000
_LOCATION_BATCH_MAX_GROUPS = 1000
_SPATIAL_BATCH_SIZE = 1000


def _estimate_group_chars(g: Dict[str, Any]) -> int:
    """估算单个 location group 在 user prompt 中的字符数（用于 batch 切分预算）

    估算项：aliases 拼接 + types 字典展开（key+count → str）+ parents 字典展开 + 固定字段标签
    实际可能偏差 ±20%，预算值已留余量。
    """
    return (
        sum(len(a) for a in g.get("aliases", [])) + 20
        + sum(len(k) + len(str(v)) + 5 for k, v in g.get("types", {}).items()) + 20
        + sum(len(k) + len(str(v)) + 5 for k, v in g.get("parents", {}).items()) + 20
        + 80  # 固定字段（chapter_count + sample_desc + 字段标签）
    )


def _split_into_batches_by_budget(
    items: List[Any],
    budget_chars: int,
    max_groups: int,
) -> List[List[Any]]:
    """按 user prompt 字符预算切分批次

    累加每个 item 的预估字符数，超过 budget_chars 或 len == max_groups 就切新批。
    """
    batches: List[List[Any]] = []
    current: List[Any] = []
    current_chars = 0
    for item in items:
        item_chars = _estimate_group_chars(item)
        if current and (current_chars + item_chars > budget_chars or len(current) >= max_groups):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(current)
    return batches


class LocationNormalizer:
    """地点与空间关系归一化（Phase 0）

    流程：
    - _run_phase_0a_batches: 切片 locations → 并发 LLM → 校验
    - _run_phase_0b_batches: 切片 spatial + canonical 白名单 → 并发 LLM
    - _run_phase_0b_dedupe: 机械去重同 (from, to)
    - 持久化到 output/{_LOCATIONS_NORMALIZED_FILENAME, _SPATIAL_NORMALIZED_FILENAME}
    - 给每章 chapter_*.json 加 _normalized_ref + _normalized_spatial_ref
    """

    def __init__(
        self,
        output_dir: Path,
        llm_client: LLMClient,
        concurrency: int = 1,
        locations_batch_size: int = _LOCATION_BATCH_MAX_GROUPS,
        spatial_batch_size: int = _SPATIAL_BATCH_SIZE,
        on_progress: Optional[Callable[[dict], None]] = None,
    ):
        self.output_dir = Path(output_dir)
        self.llm_client = llm_client
        self.concurrency = max(1, concurrency)
        self.locations_batch_size = max(1, locations_batch_size)
        self.spatial_batch_size = max(1, spatial_batch_size)
        self.on_progress = on_progress

    def stop(self) -> None:
        """请求停止当前 run() 调用（透传到 LLM 客户端）"""
        self.llm_client.request_stop()

    async def run(self) -> Optional[bool]:
        """跑完整 Phase 0
        返回：True=成功；False=失败；None=用户中途停止
        """
        # 0. 缓存短路：若两个 normalized 文件都已存在且 chapter_mtimes_hash 一致，跳过
        existing_loc, existing_rel = self._load_existing_normalized()
        current_hash = self._compute_chapter_mtimes_hash()
        if existing_loc and existing_rel:
            loc_items = existing_loc.get("locations") or []
            # 第三层防线：缓存短路要求已有 locations 非空——历史上落盘的空 normalized 文件
            # 带匹配 hash 时不能秒退，必须重新归一化给地图产出数据（P1 2026-08-24）
            if (loc_items
                    and existing_loc.get("chapter_mtimes_hash") == current_hash
                    and existing_rel.get("chapter_mtimes_hash") == current_hash):
                logger.info("Phase 0 跳过：归一化文件已是最新")
                return True
        elif existing_loc or existing_rel:
            logger.warning("归一化文件部分缺失，重新运行 Phase 0")

        if self.llm_client._stop_requested:
            logger.info("Phase 0 检测到停止请求，提前退出")
            return None

        # 1. 读取所有 chapter_*.json
        raw_locations, raw_spatial = self._load_all_chapter_data()
        if not raw_locations:
            logger.info("无 locations 数据，跳过 Phase 0")
            return False

        # 2. Phase 0a-batches：并发归一化 locations
        # 2026-08-23 激进简化：删除跨 batch consolidation 步骤（之前的卡死点）。
        # 理由：consolidation 串行单 batch，撞 630s 硬超时会让前 5 个 batch 的结果全部白做；
        # 单 batch 内的合并已经在 Phase 0a-batches 完成，跨 batch 合并收益有限。
        groups = aggregate_locations(raw_locations)
        logger.info(f"Phase 0a：{len(groups)} 个 location group")
        all_canonicals = await self._run_phase_0a_batches(groups)
        if all_canonicals is None:
            return None if self.llm_client._stop_requested else False
        # 第二层防线：即使 batch 汇总未拦住（如未来逻辑回退），也绝不能落盘空归一化数据——
        # 空白名单 + 当前 hash 会永久短路缓存，地图数据恒空且零告警（P1 2026-08-24）
        if not all_canonicals:
            logger.error("Phase 0a 所有批次均解析为零条目，视为整体失败；不落盘空归一化数据")
            if self.on_progress:
                self.on_progress({"type": "phase_failed", "phase": "0a-batches",
                                  "error": "全部批次解析为空",
                                  "message": "[地点归一化] 所有批次解析为空，请检查模型输出质量后重试"})
            return False

        # 3. 跨 batch consolidation 已删除：直接用 batch 阶段的 canonical 作为 final_locations
        final_locations = all_canonicals

        # 4. Phase 0b-batches：并发结构化 spatial
        pairs = aggregate_spatial_pairs(raw_spatial)
        logger.info(f"Phase 0b：{len(pairs)} 个 spatial pair")
        whitelist = [{"canonical": c["canonical_name"], "aliases": c["aliases"]} for c in final_locations]
        whitelist_names: Set[str] = {c["canonical_name"] for c in final_locations}
        all_spatial = await self._run_phase_0b_batches(pairs, whitelist, whitelist_names)
        if all_spatial is None:
            return None if self.llm_client._stop_requested else False

        # 5. Phase 0b-dedupe：机械去重
        final_spatial = self._run_phase_0b_dedupe(all_spatial)

        # 6. 先把 _normalized_ref 写回章节，再写 normalized 文件 —
        #    让持久化的 chapter_mtimes_hash 与最终的 chapter mtimes 一致，
        #    下次运行 hash 比对才能匹配成功
        self._add_normalized_ref_to_chapters()
        self._persist_normalized_locations(final_locations)
        self._persist_normalized_spatial(final_spatial)
        logger.info("Phase 0 完成")
        return True

    # ---- 数据读取 ----

    def _output_subdir(self) -> Path:
        sub = self.output_dir / "output"
        return sub if sub.is_dir() else self.output_dir

    def _load_all_chapter_data(self) -> tuple:
        """从 output_dir/output/chapter_*.json 聚合 locations + spatial_relationships"""
        raw_locations = []
        raw_spatial = []
        output_subdir = self._output_subdir()
        for cf in sorted(output_subdir.glob("chapter_*_result.json")):
            data = safe_load_json(cf)
            if not data:
                continue
            ch = data.get("chapter_number")
            if ch is None:
                continue
            for loc in data.get("locations", []) or []:
                loc_copy = dict(loc)
                loc_copy["chapter"] = ch
                raw_locations.append(loc_copy)
            for rel in data.get("spatial_relationships", []) or []:
                rel_copy = dict(rel)
                rel_copy["chapter"] = ch
                raw_spatial.append(rel_copy)
        return raw_locations, raw_spatial

    # ---- Phase 0a ----

    async def _run_phase_0a_batches(self, groups):
        # 2026-08-21 修复：按 user prompt 字符预算切分（不再按 group 数硬切）
        # 实测：398 groups + 53K chars user prompt → 拆 2 批避免 10+ min 超时
        batches = _split_into_batches_by_budget(
            groups,
            budget_chars=_LOCATION_PROMPT_BUDGET_CHARS,
            max_groups=self.locations_batch_size,
        )
        logger.info(
            f"Phase 0a：{len(groups)} groups → {len(batches)} batches（按字符预算）"
        )
        if not batches:
            return []
        if self.llm_client._stop_requested:
            return None
        semaphore = asyncio.Semaphore(self.concurrency)

        async def _process(batch_idx, batch):
            async with semaphore:
                if self.llm_client._stop_requested:
                    return None
                messages = build_location_prompt(batch, batch_idx, len(batches))
                try:
                    success, content, error, _tokens = await self.llm_client.chat(messages)
                except Exception as e:
                    logger.error(f"Phase 0a batch {batch_idx} 失败: {e}")
                    if self.on_progress:
                        self.on_progress({"type": "batch_failed", "phase": "0a-batches", "batch_idx": batch_idx, "error": str(e),
                                          "message": f"[地点归一化 {batch_idx}/{len(batches)}] LLM 异常"})
                    return None
                if not success:
                    logger.error(f"Phase 0a batch {batch_idx} 失败: {error}")
                    if self.on_progress:
                        self.on_progress({"type": "batch_failed", "phase": "0a-batches", "batch_idx": batch_idx, "error": error,
                                          "message": f"[地点归一化 {batch_idx}/{len(batches)}] LLM 返回失败"})
                    return None
                parsed = parse_and_validate_locations(content, batch)
                if not parsed:
                    logger.warning(
                        f"Phase 0a batch {batch_idx} 解析为空，响应前300字: {content[:300]!r}"
                    )
                if self.on_progress:
                    self.on_progress({"type": "batch_done", "phase": "0a-batches", "batch_idx": batch_idx,
                                      "total_batches": len(batches), "groups": len(parsed) if parsed else 0,
                                      "message": f"[地点归一化 {batch_idx}/{len(batches)}] 完成"})
                return parsed

        tasks = [_process(i, b) for i, b in enumerate(batches, 1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid: List[List[Dict]] = [r for r in results if isinstance(r, list)]
        # 解析成功但零条目也计入失败：HTTP 成功 + 空数组是模型违反 schema 的典型形态，
        # 按"成功"处理会固化空白名单并永久短路（P1 2026-08-24）
        empty_batches = sum(1 for r in valid if not r)
        failed = len(results) - len(valid) + empty_batches
        if failed > 0:
            logger.warning(f"Phase 0a batches: 失败/空 {failed}/{len(results)}")
        # 熔断同样按"非空批次才算成功"计算，否则全空批次绕过失败率检查直接返回 []
        non_empty_valid = [r for r in valid if r]
        if len(non_empty_valid) < len(results) * 0.5:
            logger.error(f"Phase 0a 失败率过高 ({failed}/{len(results)})，整体 abort")
            if self.on_progress:
                self.on_progress({"type": "phase_failed", "phase": "0a-batches",
                                  "error": f"失败率 {failed}/{len(results)}",
                                  "message": f"[地点归一化] 失败率过高 ({failed}/{len(results)})，整体 abort"})
            return None
        return [c for batch in valid for c in batch]

    # ---- Phase 0b ----

    async def _run_phase_0b_batches(self, pairs, whitelist, whitelist_names):
        if not pairs:
            return []
        batches = self._split_into_batches(pairs, self.spatial_batch_size)
        if self.llm_client._stop_requested:
            return None
        semaphore = asyncio.Semaphore(self.concurrency)

        async def _process(batch_idx, batch):
            async with semaphore:
                if self.llm_client._stop_requested:
                    return None
                messages = build_spatial_prompt(batch, whitelist, batch_idx, len(batches))
                try:
                    success, content, error, _tokens = await self.llm_client.chat(messages)
                except Exception as e:
                    logger.error(f"Phase 0b batch {batch_idx} 失败: {e}")
                    if self.on_progress:
                        self.on_progress({"type": "batch_failed", "phase": "0b-batches", "batch_idx": batch_idx, "error": str(e),
                                          "message": f"[空间关系 {batch_idx}/{len(batches)}] LLM 异常"})
                    return None
                if not success:
                    logger.error(f"Phase 0b batch {batch_idx} 失败: {error}")
                    if self.on_progress:
                        self.on_progress({"type": "batch_failed", "phase": "0b-batches", "batch_idx": batch_idx, "error": error,
                                          "message": f"[空间关系 {batch_idx}/{len(batches)}] LLM 返回失败"})
                    return None
                parsed = parse_and_validate_spatial(content, whitelist_names)
                if not parsed:
                    logger.warning(
                        f"Phase 0b batch {batch_idx} 解析为空，响应前300字: {content[:300]!r}"
                    )
                if self.on_progress:
                    self.on_progress({"type": "batch_done", "phase": "0b-batches", "batch_idx": batch_idx,
                                      "total_batches": len(batches), "groups": len(parsed) if parsed else 0,
                                      "message": f"[空间关系 {batch_idx}/{len(batches)}] 完成"})
                return parsed

        tasks = [_process(i, b) for i, b in enumerate(batches, 1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid: List[List[Dict]] = [r for r in results if isinstance(r, list)]
        failed = len(results) - len(valid)
        if failed > 0:
            logger.warning(f"Phase 0b batches: {failed}/{len(results)} 失败")
        if len(valid) < len(results) * 0.5:
            logger.error(f"Phase 0b 失败率过高 ({failed}/{len(results)})，整体 abort")
            if self.on_progress:
                self.on_progress({"type": "phase_failed", "phase": "0b-batches",
                                  "error": f"失败率 {failed}/{len(results)}",
                                  "message": f"[空间关系] 失败率过高 ({failed}/{len(results)})，整体 abort"})
            return None
        return [r for batch in valid for r in batch]

    @staticmethod
    def _run_phase_0b_dedupe(all_spatial):
        """同 (from, to) 对的多 batch 结果合并：evidence_chapters 取并集"""
        pair_meta = {}
        for rel in all_spatial:
            key = (rel["from"], rel["to"])
            if key not in pair_meta:
                pair_meta[key] = dict(rel)
            else:
                existing = pair_meta[key]
                existing["evidence_chapters"] = sorted(set(
                    existing["evidence_chapters"] + rel["evidence_chapters"]
                ))
        return list(pair_meta.values())

    # ---- 工具 ----

    @staticmethod
    def _split_into_batches(items, batch_size):
        return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]

    # ---- 持久化 ----

    def _persist_normalized_locations(self, locations) -> None:
        output_subdir = self._output_subdir()
        payload = {
            "schema_version": 1,
            "normalized_at": datetime.now().isoformat(timespec="seconds"),
            "model": (
                self.llm_client.config.model
                if hasattr(self.llm_client, "config")
                else getattr(self.llm_client, "model", "unknown")
            ),
            "chapter_mtimes_hash": self._compute_chapter_mtimes_hash(),
            "locations": [
                {
                    "canonical_name": loc["canonical_name"],
                    "aliases": loc["aliases"],
                    "parent": loc.get("parent", ""),
                    "type": loc.get("type", ""),
                    "description": loc.get("description", ""),
                    "chapter_count": loc.get("chapter_count", 0),
                }
                for loc in locations
            ],
        }
        out_path = output_subdir / _LOCATIONS_NORMALIZED_FILENAME
        if not safe_save_json(payload, out_path):
            raise RuntimeError(f"Phase 0 落盘失败: {out_path}")

    def _persist_normalized_spatial(self, spatial) -> None:
        output_subdir = self._output_subdir()
        payload = {
            "schema_version": 1,
            "normalized_at": datetime.now().isoformat(timespec="seconds"),
            "model": (
                self.llm_client.config.model
                if hasattr(self.llm_client, "config")
                else getattr(self.llm_client, "model", "unknown")
            ),
            "chapter_mtimes_hash": self._compute_chapter_mtimes_hash(),
            "relationships": spatial,
        }
        out_path = output_subdir / _SPATIAL_NORMALIZED_FILENAME
        if not safe_save_json(payload, out_path):
            raise RuntimeError(f"Phase 0 落盘失败: {out_path}")

    def _compute_chapter_mtimes_hash(self) -> str:
        output_subdir = self._output_subdir()
        mtimes = []
        for cf in sorted(output_subdir.glob("chapter_*_result.json")):
            mtimes.append(f"{cf.name}:{int(cf.stat().st_mtime)}")
        return hashlib.md5("\n".join(mtimes).encode("utf-8")).hexdigest()

    def _load_existing_normalized(self) -> tuple:
        """读已存在的 normalized 文件，返回 (locations, spatial)；不存在则对应项为 None。"""
        output_subdir = self._output_subdir()
        loc_path = output_subdir / _LOCATIONS_NORMALIZED_FILENAME
        rel_path = output_subdir / _SPATIAL_NORMALIZED_FILENAME
        loc = safe_load_json(loc_path) if loc_path.exists() else None
        rel = safe_load_json(rel_path) if rel_path.exists() else None
        return loc, rel

    def _add_normalized_ref_to_chapters(self) -> None:
        """给每章 chapter_*.json 顶部加 _normalized_ref + _normalized_spatial_ref 字段"""
        output_subdir = self._output_subdir()
        for cf in output_subdir.glob("chapter_*_result.json"):
            data = safe_load_json(cf)
            if not data:
                continue
            data["_normalized_ref"] = _LOCATIONS_NORMALIZED_FILENAME
            data["_normalized_spatial_ref"] = _SPATIAL_NORMALIZED_FILENAME
            if not safe_save_json(data, cf):
                raise RuntimeError(f"Phase 0 章节标记落盘失败: {cf}")
