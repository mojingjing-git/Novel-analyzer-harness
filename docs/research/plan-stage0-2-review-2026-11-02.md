# 2.0 阶段 0-2 计划复审报告（正文身份落到文件级）

> 评审对象：`docs/PLAN-2.0-stage0-2026-11-02.md`  
> 评审日期：2026-11-02  
> 评审结论：**需要修改后执行**（方案 B 大方向成立，但存在账本字段污染、全书回退性能陷阱、Prompt 标签矛盾三大设计缺陷，必须补齐约束后再开工）。

---

## 1. 代码事实核实（抽查 15 处）

| # | 核查对象 | 计划陈述 | 代码实际情况 | 结论 |
|---|---|---|---|---|
| 1 | `backend/services/recheck_v2.py:266-280` | 先按章号找，找不到退回全书回退搜索 | `located = locate_quote(quote, source)`；若失败且有 `global_text`，调 `locate_quote(quote, global_text)` 并通过 `chapter_spans` 反查归属 | **属实** |
| 2 | `backend/services/recheck_v2.py:196, 220, 239-245` | `chapters` 参数用于建索引与全书拼装 | 传入 `chapters: Mapping[int, str]`，用于构建 `EvidenceIndex(chapters)`，并遍历排序键拼出 `global_text` 与 `chapter_spans` | **属实** |
| 3 | `backend/services/recheck_v2.py:220, 321, 337` | `EvidenceIndex` 用法（检索与取窗） | 调 `index.chapter_text` 定位整章，调 `index.bundle_for` 获取窗口证据包（内部组合 `window` 与 `find_mentions`） | **属实** |
| 4 | `backend/services/recheck_v2.py:118, 176` | `resolved_chapters` 字段是否存在 | `RecheckOutcome.resolved_chapters: dict[str, int]` 明确定义，并在 `dump_recheck_detail` 中序列化导出 | **属实** |
| 5 | `backend/services/final_summary.py:1414-1436` | `_collect_chapters` 怎么建章映射 | 遍历 `blocks/*.txt`，对每个文件调 `split_block_into_chapters(text, ordinal)`，将返回的字典逐项写入 `chapters: dict` | **属实** |
| 6 | `scripts/measure_recheck_v2_pipeline.py:278-285` | 测量脚本怎么建章映射 | 遍历 `blocks/*.txt`，调 `split_block_into_chapters(text, ordinal)` 并执行 `chapters.update(...)` | **属实** |
| 7 | `backend/core/evidence.py:90-146, 147-204` | `EvidenceIndex` 按文本单元检索、换文件不用改 | 索引将 key 视为无特殊业务语义的整数 ID，`window` 按字符截取，`find_mentions` 做词频加权与 ID 比较，将 key 换成 `file_index` 核心逻辑完全兼容 | **属实** |
| 8 | `backend/core/anchor.py:89-104, 224-233` | `split_block_into_chapters` 与 `build_chapter_spans` 差异 | `build_chapter_spans` 返回保序的 `list[tuple[int, int, int]]` 保留起止；`split_block_into_chapters` 返回 `dict[int, str]`，同号章会被后者直接静默覆盖 | **属实** |
| 9 | `backend/utils/text_utils.py:246-255` | 位置契约第 6..10 位 | `extra = list(item[5:10])`，分别取出 `anchor_quote`, `anchor_chapter`, `motif_key`, `question`, `answer_criteria` | **属实** |
| 10 | `backend/core/analyzer.py:233-235` | 采集侧记录伏笔锚点 | 调用 `verify_result_anchors(result, chapter_content, first_chapter, min_chars)` | **属实** |
| 11 | `backend/core/anchor.py:152, 169` | `verify_result_anchors` 反查章号 | 内部调用 `build_chapter_spans(block_text, first_chapter)` 反查章号 | **属实** |
| 12 | `backend/models/analysis_result.py:32-50` | 结果模型字段 | `Foreshadowing` 包含 `anchor_quote`, `anchor_chapter`, `motif_key`, `question`, `answer_criteria`, `lead` | **属实** |
| 13 | `backend/utils/foreshadow_ledger.py:24` | 账本 Schema 版本号 | 明确定义常量 `SCHEMA_VERSION = 5` | **属实** |
| 14 | `backend/services/recheck_v2.py:181-189` | `_evidence_id` 哈希规则 | `f"{chapter}\x1f{text}".encode("utf-8")` 取 sha256 前 12 位 | **属实** |
| 15 | `backend/services/recheck_v2.py:486, 650-658` | 账本状态回写逻辑 | `_apply_state` 内部 `item.resolved_chapter = int(located_chapter or ...)`，会把 `chapter_by_evidence` 的值直接写入账本 | **有出入**（计划声称“不碰账本、不改 schema”，但若把证据章号换成 `file_index`，此处会把文件序号直接写进账本的 `resolved_chapter`） |

---

## 2. 方案 B 可行性深度判断

### 2.1 "文件 = 一段正文"在 1.x blocks 约定下是否成立？
- **物理事实**：成立。`backend/core/file_processor.py:40-64` 要求 `blocks/` 目录下必须是纯数字命名的独立文件（`1.txt`, `2.txt` ...），每个文件确实是一段物理正文。
- **业务假设与例外**：
  1. 在规范切分下，`book_service.py:183-184` 默认 `total_blocks == total_chapters`。
  2. 但在 1.x 实际场景中，**并不保证一个文件只含一章**：源文本未切净、超大章被拆分（`splitter_service.py:45`）、或多章合并存放，都是客观存在的。
- **对方案 B 的影响**：
  - 方案 B 把检索单元从“文学章号”降级为“物理文本文件”，解耦了不稳定的标题切分，这是正确的转向。
  - **边界缺陷**：如果整本书未分章（极端情况下只有 1 个大文件），`EvidenceIndex.find_mentions(after=file_index)` 的 `after` 过滤会导致 `after=1` 之后没有任何文件，**同一文件内的跨章/后文回收将被彻底漏查**。若单文件出现多章，当前方案无法检索同文件内的后文。

### 2.2 把 `EvidenceIndex` 的"章"换成"文件"后，取窗与提示词语义是否改变？
- **取窗半径**：未变。`backend/core/evidence.py:20, 140-141` 中 `WINDOW_RADIUS = 400` 是**字符数**，取窗是基于正文字符偏移前后各取 400 字，不会因文件变大而膨胀。
- **提示词标签严重漂移（重大隐患）**：
  - `evidence.py:174, 191` 会生成 `【第 {anchor_chapter} 章（埋设处）】`；
  - `recheck_v2.py:364` 会生成 `[evidence_id={eid} 第{seg["chapter"]}章]`；
  - 若传入的是 `file_index`，展示给 LLM 的将是 `[evidence_id=... 第366章]`；
  - 但候选条目的元数据（`recheck_v2.py:354-355`）展示的依然是采集侧的章号：`首次出现：第12章；最后提及：第15章`。
  - **后果**：LLM 看到首次出现在第 12 章，证据段却被标为“第 366 章”，会引发严重认知冲突（认为证据给错或编号混乱），甚至导致拒绝回答或回填错误。

### 2.3 "引文取全书最早出现"语义是否准确？
- **大部分成立，但存在确定性反例**：
  - **反例 1（小说常见套话/俗语/动作描写）**：模型截取的 `anchor_quote` 偶尔是常见行文（如“只见那人身材魁梧”、“第二日清晨天刚破晓”），若该短语在全书前文出现过，全书盲搜会误挂到全书最开头完全无关的场景。
  - **反例 2（跨度巨大且无 Sanity Check）**：一条伏笔采集时记录在第 800 回附近，全书搜索若命中第 2 回的同名短语，方案 B 会毫无察觉地认定其埋在第 2 个文件，导致取窗完全偏离。

### 2.4 全书搜索性能与致命陷阱（`difflib` 计算爆炸）
- **C 语言 `str.find` 的实际耗时**：435 万字符 × 1669 条引文，纯 `str.find` 是 Boyer-Moore-Horspool 的 C 优化实现，全量扫描仅需 1~3 秒，纯字面搜索完全可行。
- **致命性能陷阱（必须拦截）**：
  - 查看当前 `backend/services/recheck_v2.py:269`：
    ```python
    found = locate_quote(quote, global_text)
    ```
  - 查看 `backend/vnext/resolution.py:284` 与 `backend/core/citations.py:174`：
    当某条引文在全书中**字面找不到**（例如模型有幻觉或改动标点的条目）时，`locate_quote` 会回退调用 `citations.verify_citation`，其内部在行 174 会执行：
    ```python
    matcher = difflib.SequenceMatcher(None, normalized_quote, normalized_chapter)
    block = matcher.find_longest_match(0, len(normalized_quote), 0, len(normalized_chapter))
    ```
  - **`difflib.SequenceMatcher` 在 4.35M 字符的全书文本上运行，算法复杂度为 $O(N \cdot M)$！单次调用将霸占 CPU 数十秒至数分钟，几十条缺失引文会直接导致复检管线永久卡死！**
  - **结论**：全书搜索**绝不能直接调用 `locate_quote`**，必须改用纯字面快速匹配（`str.find`）或预构建字符偏移索引，严禁触碰 `difflib`。

---

## 3. 计划挑毛病与盲点审查

### 3.1 "不改动"清单的自相矛盾：账本字段被隐式污染
- 计划在“2. 两个方案”与“6. 不做什么”中明确承诺：**不动采集侧，不改账本 schema，不改动 `foreshadow_ledger.py`**。
- 但查看 `backend/services/recheck_v2.py:486, 650-658`：
  ```python
  item.resolved_chapter = int(located_chapter or getattr(item, "anchor_chapter", 0) or 0)
  ```
- 这里的 `located_chapter` 来自 `chapter_by_evidence`（即 `cand["chapter"]`）。在方案 B 改成 `file_index` 后，**`item.resolved_chapter` 会被直接赋值为文件序号（例如 366）**！
- **后果**：
  1. 账本数据被污染：`item.first_seen_chapter` 存的是文学章号（如 12），`item.resolved_chapter` 存的却是文件序号（如 366），两个字段跨越了不同的坐标空间。
  2. 账本下游的休眠跨度计算（`DORMANT_CHAPTER_THRESHOLD = 200`）、最终总结报表、前端“在第 N 章回收”的展示全部出现错位。
- 计划既不想改 schema，又想在 `recheck_v2.py` 内部偷换概念，构成了严重的内部矛盾。

### 3.2 改动点 4 与 5 的重复与口径割裂风险
- 计划改动点 4（`final_summary.py:_collect_chapters`）和改动点 5（`measure_recheck_v2_pipeline.py`）要求各自修改 `blocks` 遍历逻辑。
- 这违反 DRY 原则，容易因排序规则（如字典序 `1.txt, 10.txt` vs 自然数序 `1, 2, ... 10`）、文件过滤（是否忽略非数字 txt）不同而产生生产与测量口径不一致。
- **整改要求**：必须在 `backend/core/file_processor.py` 中抽象统一的公共函数 `load_blocks_as_files(blocks_dir: Path) -> dict[int, str]`，两处共同调用。

### 3.3 验收标准存在同义反复（无法作为门禁）
- 计划验收标准写道：“文件级定位率：100%（引文逐字存在的那部分）”。
- “引文逐字存在的那部分”是未形式化定义的模糊兜底：如果定位不成功就被归为“引文非逐字存在”，则该指标在逻辑上恒为 100%（定义式通过）。
- **客观门禁标准**：必须固定输入候选（如 1669 条），产出确凿的分类断言：
  1. `located_count`: 精确匹配到 `file_index` 的数量（如 1464 条）；
  2. `absent_count`: 经全书快速字面扫描确认全书不存在的数量（如 205 条）；
  3. `failed_unresolved_count`: 全书存在引文字面但未定位到文件的数量，**必须严格为 0**。

### 3.4 风险表遗漏项
1. **全书搜索触发 `difflib` 卡死的计算崩溃风险**（未提及）。
2. **账本 `resolved_chapter` 被注入文件序号的语义破坏风险**（未提及）。
3. **Prompt 提示词中文件序号伪装成章号导致 LLM 困惑的风险**（未提及）。
4. **单文件多章时 `find_mentions(after=file_index)` 遗漏同文件回收的风险**（未提及）。

---

## 4. 结论与分级整改建议

### 一句话结论
**需要修改后执行**：方案 B 采用物理文件解耦切分章号的方向正确且成本最低，但现计划遗漏了账本字段被文件序号污染、全书搜索潜伏 `difflib` 性能爆炸、以及 Prompt 中文件序号冒充章号三大关键隐患，必须修补设计后才能动工。

### P1 / P2 / P3 分级表

| 级别 | 问题与修改动作（每条一句话） |
|---|---|
| **P1** | 严禁在全书字符串上调用带 `difflib` 的 `locate_quote`，必须实现专用无回退的轻量全书字面定位器以防进程卡死。 |
| **P1** | 明确账本 `item.resolved_chapter` 的写入契约，绝不能将 `file_index` 直接赋给章号字段以防污染生产账本。 |
| **P1** | 修正 Prompt 与 Evidence 渲染模板，显式区分“文件序号”与“章节号”，避免 LLM 因信息冲突误判。 |
| **P2** | 抽离统一的 `load_blocks_as_files` 公共函数，消除 `final_summary.py` 与测量脚本的双重维护和排序口径漂移。 |
| **P2** | 将验收标准从模糊的“100%”重写为带确切分母（1669）与三态分类（命中/确凿缺失/异常丢失）的客观离线断言。 |
| **P3** | 全书引文搜索增加跨度合理性保护（Sanity Check），对短引文命中过早章节或常见套话进行可信度标记。 |

---

## 5. 开工前必须先定的事

1. **定死账本中 `resolved_chapter` 的口径**：
   - 方案 1（兼容推荐）：利用 `chapter_spans` 反查回原书章号写入 `resolved_chapter`，同时在明细和账本中扩展可选字段 `resolved_file_index` 记录物理文件。
   - 方案 2：正式将 `resolved_chapter` 扩义为 `resolved_unit`，并同步修改报表与前端显示逻辑。
   - **不能不做决定就直接往 `item.resolved_chapter` 里塞 `file_index`。**
2. **定死全书搜索的算法边界**：
   - 明确全书引文搜索只允许使用 C 层面的纯文本 `str.find` 或预建的轻量偏移倒排表，彻底切断 `difflib` 模糊匹配在全书文本上的执行路径。
3. **定死 Prompt 的展示标签规范**：
   - 决定传给模型的证据块标题是叫 `【文件 366】` 还是继续混淆叫 `【第 366 章】`，确保 Prompt 全文术语一致。
