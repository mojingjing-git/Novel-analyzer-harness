---
doc_type: plan
status: draft
---

# 切章器 DOCX 输入支持 — 实施计划

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-28（v2，已按 reviewer 复审意见修订） |
| **目标** | 让切章器支持 `.docx` 输入（零新增第三方依赖，纯标准库） |
| **验收** | DOCX 与同内容 TXT 的**切分结构一致**（index/title/volume/num/detected_pattern），见 §1 A 验收的修订版口径 |
| **范围** | 仅 DOCX。**不含** EPUB（见 §7 不做清单） |
| **核心策略** | 入口归一化：在 `_detect_and_read` 单一位置把 DOCX 转成 str，下游零改动 |
| **复审状态** | v1 已过 agent 复审；3 个阻塞项 + 2 个重要项已在本版修订（修订记录见 §10） |

---

## 0. 背景与关键判断

### 0.1 为什么接入点异常干净

`splitter_service.py:273` 的 `_detect_and_read()` 是全链路**唯一**的文本入口：

```python
def _detect_and_read(file_path: Path) -> str:
    content = detect_and_decode(file_path, ENCODING_CANDIDATES)
    if content.startswith("\ufeff"):
        content = content[1:]
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return content
```

调用方仅两处（`preview_split:1067`、`save_split:1101`）。下游 1243 行切章逻辑
（15 章正则 / 5 卷正则 / 去重 / TOC 跳过 / 元数据提取）消费的都是它吐出的 `str`。

**结论：只要在这个函数里加一层"DOCX → 纯文本"，下游一行都不用改。**

### 0.2 为什么大量 `.txt` 依赖不受影响（关键澄清）

仓库里有一批 `glob("*.txt")`：

| 位置 | 读的是什么 | 是否需改 |
|---|---|---|
| `queue_manager.py:250/277` | `blocks/*.txt` 计数 | ❌ 否 |
| `book_service.py:176` | `blocks/*.txt` 计数 | ❌ 否 |
| `file_processor.py:53/171` | `blocks/NNNN.txt` 正文 | ❌ 否 |
| `style_analyzer.py:204/404` | `blocks/*.txt` 采样 | ❌ 否 |

**理由**：这些全是切分**之后**读 `blocks/` 里的产物。DOCX 一旦过了
`_detect_and_read`，就会被 `save_split` 写成同样的 `NNNN.txt` 落盘。
"先归一化、再进流水线"把 blast radius 压到了最小 —— 这是本方案成立的前提。

### 0.3 只动的生产文件清单（3 个）

| 文件 | 改动 |
|---|---|
| `backend/utils/docx_extract.py` | **新增**：DOCX → 纯文本（约 120 行） |
| `backend/services/splitter_service.py:273` | `_detect_and_read` 按后缀分派（约 8 行） |
| `desktop.py:361` | `file_types` 追加 DOCX 过滤器（约 1 行） |

另需动 `frontend/src/pages/SplitterPage.vue:39`（`baseName()` 的
`replace(/\.txt$/i, '')` 需认新后缀）。

**不需要**动：`requirements.txt`（纯标准库）、REST 契约（`file_path` 字段本就接受任意字符串，无需改签名）。

---

## 1. 验收标准（Definition of Done）

✅ **必须全部满足**才算完成：

- [ ] **A 验收（核心，v2 修订）**：同一本书准备 TXT / DOCX 两个版本，走 `save_split`
      落到两个临时目录，断言：
      - **强一致（必须逐字节相同）**：`total_chapters`、每块的 `index` / `title` /
        `volume` / `num`、`detected_pattern`、`pattern_name`
      - **显式排除**：`word_count` **不做相等断言**（理由见下方▲ 阻塞项 1）
      - **附加断言**：两条路径的 `detected_pattern` 必须相同 —— 这是防止"自动检测误
        判"的最敏感探针（理由见▲ 阻塞项 2）

      > **▲ 关于 word_count 的重要说明**：`word_count` 名不副实，实为 `len(content)`
      > 的**字符数**（`splitter_service.py:862` `word_count=len(body)`）。DOCX 路径
      > 因 D2 注入换行必然导致字符数上浮，故从五元组中移除。真正影响下游的是
      > index/title/volume/num 四元组与 `detected_pattern`。

- [ ] **B 验收**：`preview_split` 与 `save_split` 两条链路都能处理 DOCX（两入口全覆盖）
- [ ] **C 验收**：全部现有 pytest **无回归**（443 用例，以 `--collect-only` 实时数为准）
- [ ] **D 验收**：新增测试文件 `backend/tests/test_splitter_docx.py`，覆盖 §4 的 6 类 case
- [ ] **E 验收（v2 拆分）**：见下方 §1.1 的 E1 / E2 两项**分别**定义，二者不再是同一件事
- [ ] **F 验收**：`desktop.py` 的文件选择对话框能筛出 `.docx`；`SplitterPage.vue`
      文件名去后缀正确、`<input accept>` 能选中 docx

### 1.1 E 验收的拆分（▲ 阻塞项 3 的落地）

| 子项 | 场景 | 期望行为 | 由谁兜底 |
|---|---|---|---|
| **E1** | 真正的空输入：0 字节 / `w:body` 无任何文本 | 抛明确 `ValueError` | T2.3（`_detect_and_read` 层） |
| **E2** | 有内容但**识别不出任何章节行** | **继承现有 TXT 行为**（回落为单章"全文"），**不新增抛错** | `split_text:835-844` 既有 fallback |

**为什么必须拆开**：`split_text:835` 存在显式的 `if not raw_blocks:` fallback ——
"无法识别则整体作为单章"。这是 TXT 路径的既有行为，**如果为了 DOCX 去改它，会直接
破坏"无回归"的 C 验收**。

**决策**：E2 **不做特殊处理**。理由：(1) 与 TXT 行为保持一致，用户预期统一；
(2) `preview_split` 已返回 `detected_pattern` 与 `total_chapters=1`，前端可见；
(3) 改动该 fallback 的回归风险远大于收益。
此决定需在交付说明中显式写出，避免后续 Agent 误判为漏修。

**不做（显式排除）**：EPUB 解析、`.doc`（老二进制格式）、ODT、PDF、RTF。

---

## 2. 设计决策与已裁决问题

### D1：依赖选型 —— 手写，不用 python-docx

**裁决**：纯标准库 `zipfile` + `xml.etree.ElementTree`。

| 方案 | 取舍 |
|---|---|
| **手写（选此）** | 约 120 行；零依赖；只读我们需要的部分；可控标题 heuristic |
| `python-docx` | 多一行 import，但拖进 `lxml` 大依赖（PyInstaller 打包体积↑），而我们要的功能只占其极小一部分 |

这与项目现有风格一致：`chardet` / `jieba` 都是**可选依赖，缺失时优雅降级**。

### D2：换行边界 —— 最大风险点

切章器重度依赖**行首正则匹配**（`chapter_rx.match(s) and 0 < len(s) <= 80`）。
DOCX 里「第一章 xxx」常常是**段落样式**（Heading 1）而非独立文字行。若标题与正文
被合并进同一行 → 正则失配 → 章节被吞。

#### ▲ v2 重要修订：砍掉「短行启发」，先只做样式检测

v1 主张"双触发 heuristic（样式兜底 + ≤30 字短行启发）"，并断言"多断行最多产生若干
空行，无害"。**复审证明这个判断是错的**，两个理由：

1. **贪心断行不是产生空行，而是切割正文**。
   "≤30 字且无句末标点"这一条会命中大量**普通的短对话段落**（网文里极常见，如
   `"住手。"` / `他点头。`），在它们之前强行插入换行，会把本属于正文的短句变成伪标题。

2. **更隐蔽的危害：污染自动检测采样窗口**。
   `_chapter_regex_for_mode` 只扫**前 800 行**（`scan_lines: int = 800`，
   `splitter_service.py:261`），且要求 `scores[best_name] >= 5` 才认（`line 497`）。
   短行启发每插一个换行就多占一行，同一本书的 DOCX 版本行数会膨胀 2-4 倍 →
   **前 800 行里真实章节标题的密度被稀释** → 可能选不出最优正则，
   `detected_pattern` 与 TXT 不一致 → 切出**完全不同的章节结构**。
   这正是 §10 记过的「静默把整本书当 1 章」的同类事故路径。

**裁决（v2）**：

- **Phase 1 只实现「样式检测」**：段落 `w:pStyle` 匹配 `Heading*`（含 `Heading 1/2/3`、
  `heading 1`）或 `Title`，不分大小写。用**正则匹配 `^heading\s*\d*$|^title$`**，
  不要只比对 T1.1 原先列的三个字面量（Word 还会产出 Heading 2/3/4）。
- **短行启发推迟**：仅在拿到真实 DOCX 样本、且**证实样式检测确实漏抓标题**时才加，
  并必须同步补一条"前 800 行仍能选出正确 pattern"的回归用例。
- **补偿措施**：无论如何，**Case 1 必须断言两条路径的 `detected_pattern` 相同**，
  把"自动检测是否被带偏"变成一道硬闸门。

### D3：处理顺序 —— 与现有 `_detect_and_read` 保持一致

```
docx → 抽取 XML → 拼 str → 统一换行 → 剥 BOM
```

注意**顺序不能反**：DOCX 内容已被 XML 处理，其中的 BOM 与换行符由 XML 层产出，
因此必须在**拼接完成后**再走与 TXT 相同的 `.replace("\r\n","\n")` 归一，确保两条路径
产出的归一化结果严格一致。

### D4：难点 —— 多段落合并 vs 逐段落

DOCX 常见「一段多句」。若逐 `<w:p>` 断行，一个物理段落被拆成多行，不影响章节识别
（正文本就可跨行），但会让行数虚高。**裁决**：逐段落一行，不做二次合并，保持与 TXT
"每行一个自然段"的形态最接近。

---

## 3. 任务清单（Checklist）

### Phase 1：核心抽取（预计 1.5 天）

- [ ] **T1.1** — 新增 `backend/utils/docx_extract.py`
  - `extract_docx_text(file_path: Path) -> str`（主入口）
  - `_iter_body_paragraphs(root) -> Iterator`（**用 `body.iter(qn('w:p'))` 递归遍历**，
    而非只取 `w:body` 的直接子节点 —— 见下方 ▲ 重要项 5）
  - `_paragraph_text(p) -> str`（取全部 `w:t`；`w:tab` → 空格；`w:br` → `\n`）
  - `_is_heading(p) -> bool`（D2 样式检测，正则 `^heading\s*\d*$|^title$`）
- [ ] **T1.2** — 用 zipfile 打开 `word/document.xml`；缺失该文件 → 抛明确错误（"不是有效的 DOCX"）
- [ ] **T1.3（v2 细化）** — 异常分诊，必须**按下列顺序**处理：
  1. 先读开头 4 字节校验 **ZIP magic `PK\x03\x04`** → 不符即"不是 DOCX/文件损坏"（区别于损坏 zip）
  2. catch `zipfile.BadZipFile` → "文件已损坏"
  3. catch **`RuntimeError`** → **"文档已加密，无法读取"**。⚠️ 关键：加密 docx 抛的
     是 `RuntimeError`（password required）**而不是 `BadZipFile`**，v1 漏了这一条
  4. 可选：`ZipFile.testzip()` 二次校验
  - 全部转成 **`ValueError`**，因为 HTTP 层只认 `ValueError` → 400，
    其余异常会被 `routes_splitter.py:94` 的 `except Exception` 吞成 **500**
- [ ] **T1.4** — 命名空间用 `{http://schemas.openxmlformats.org/wordprocessingml/2006/main}`，不硬编码前缀

> **▲ 重要项 5（v2 新增）：为什么必须递归 `.iter()`**
> 网页转制的小说 DOCX 常把整章正文塞进**单单元格表格**；只取 `w:body` 直接子节点会
> 完全漏掉，抽到空文本 → E1 报错 → 用户拿到一本"无法解析"。改用
> `body.iter(qn('w:p'))` 递归取全部段落，几乎零额外成本就能覆盖表格内正文
> （文档序通常即阅读序）。这也把 §5 里"表格内容不抽"那条风险从"中"降到"低"。

### Phase 2：接入切章器（预计 0.5 天）

- [ ] **T2.1** — 改 `splitter_service.py:273` `_detect_and_read`：`.docx`（不区分大小写）→ `extract_docx_text`，否则走原 `detect_and_decode`
- [ ] **T2.2** — 统一后处理：剥 BOM + 换行归一（抽成局部逻辑，两条分支共用）
- [ ] **T2.3** — 空文本防御：抽取结果 strip 后为空 → 抛明确 `ValueError`

### Phase 3：宿主与前端（预计 0.5 天）

- [ ] **T3.1 — `desktop.py:361`**：`file_types` 从 `("TXT 文件 (*.txt)",)` 改为
      `("TXT 文件 (*.txt)", "Word 文档 (*.docx)")`。
      ⚠️ **必须写成两个并列字符串**，若照 v1 写成嵌套元组
      `("TXT...", ("Word...",))` 会因元素是元组而失效（pywebview 要求每个元素为字符串）
- [ ] **T3.2 — `SplitterPage.vue:39`**：`baseName()` 的 `replace(/\.txt$/i, '')`
      改为同时剥离 `.docx`
- [ ] **T3.3（v2 补齐，重要项 4）** — **`SplitterPage.vue:197`** 的
      `<input type="file" accept=".txt">` 改为 `accept=".txt,.docx"`。
      这是 v1 **漏掉的第二处前端过滤**：只改 `desktop.py` 的话，浏览器 fallback 路径
      （非桌面 pywebview 环境）依然选不到 docx 文件
- [ ] **T3.4** — UX 确认：加第二个过滤器后对话框会多出"文件类型"下拉框且不再默认
      TXT-only —— 需确认可接受，否则改为单一组合过滤器
      `"文本文件 (*.txt;*.docx)"`

### Phase 4：测试与回归（预计 1 天）

- [ ] **T4.1** — 新建 `backend/tests/test_splitter_docx.py`
- [ ] **T4.2** — 合成 DOCX fixture 生成器（测试内 `zipfile` 内存构造，不入库二进制）
- [ ] **T4.3** — TXT/DOCX 等价性断言（A 验收 v2 口径：**不含** `word_count`，**含** `detected_pattern`）
- [ ] **T4.4** — 跑全量 pytest 确认无回归（C 验收，基线 **443** 用例）
- [ ] **T4.5** — `npm run build` + `npm test`（前端改动后必备）
- [ ] **T4.6 — v2 新增**：处理**根目录 `tests/` 的遗留问题**。`pytest.ini` 的
      `testpaths = backend/tests` 使得根目录 `tests/test_splitter_edge_cases.py` 与
      `test_splitter_english_baseline.py` **从不参与默认收集**（故 443 不含它们）。
      本轮**不修**（避免范围蔓延），但需在 `agent.md` 变更条目里**显式记录这个事实**，
      防止后续 Agent 误以为全量已覆盖

### Phase 5：文档同步（⚠️ 项目硬性约束）

- [ ] **T5.1** — 更新 `agent.md`：§2 仓库结构（新增 `utils/docx_extract.py`）、§5.1/§5.3 模块说明、§10 追加变更条目
- [ ] **T5.2 — v2 细化**：`README.md` 有**多处**需改，不止"功能列表与安装段"：
      - line 13 功能列表「切分 txt」
      - line 115 使用流程描述
      - 安装段补充"DOCX 无需额外依赖"
- [ ] **T5.3** — 更新 `docs/architecture.md`：
      - 数据流全景图首节点 `.txt` → `.txt / .docx`
      - 该图对应的 **mermaid 节点（`architecture.md:49`）同样要改**，别只改文字表
- [ ] **T5.4** — `CHANGELOG.md` 追加条目

---

## 4. 测试清单（`test_splitter_docx.py`）

沿用现有 `test_splitter_*` 风格：模块 docstring 说明 bug 背景 + `sys.path.insert` +
pytest 纯函数优先。

| # | Case | 断言 |
|---|---|---|
| 1 | **等价性**：同内容 TXT vs DOCX | `total_chapters` + `index/title/volume/num` 强一致<br>+ **`detected_pattern` 必须相同**（新增，防自动检测被带偏）<br>+ `word_count` **不断言**（字符数，见 §1） |
| 2 | Heading 样式段落 | 被识别为独立行（不吞标题）；覆盖 Heading 1/2/3 与 Title 多种样式名 |
| 3 | 表格内正文（网页转制场景） | 递归 `.iter()` 能抽到，不返回空（原 v1 Case 3 已改为表格场景） |
| 4 | 含 `<w:tab>` / `<w:br>` | tab 转空格、br 转换行，不丢字符 |
| 5 | 多段落正文 | 逐段落一行，行数与段落数一致 |
| 6a | **异常 E1**：0 字节 / 非 zip / 损坏 zip / 空 `w:body` | 抛 `ValueError`（→ HTTP 400，非 500） |
| 6b | **异常 E1**：**加密 docx** | 抛 `ValueError` 且 message 含"加密"字样（验证 `RuntimeError` 分支真的走到了） |
| 7 | **回归探针**：自动检测未被污染 | 构造 ≥200 段的长 DOCX，断言 `detected_pattern` 与 TXT 相同（守 §D2 的 800 行窗口风险） |

Case 6a/6b 是重点 —— 对应 §10 记录过的「静默失败」历史事故类型，必须显式拦截。
Case 7 是 v2 新增，直接对应阻塞项 2。

---

## 5. 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| D2 标题未断行 → 吞章 | **中高** | 样式检测 + Case 2 用例 + Case 1 的 `detected_pattern` 一致性硬断言 |
| 短行启发污染 800 行采样窗口 → 选错正则 | **中高**（v1 遗漏） | v2 已**移除**该启发式；将来若重加必须有 Case 7 护航 |
| **表格内正文**导致抽到空文本 | 中 → **低** | T1.1 改用 `body.iter(qn('w:p'))` 递归遍历（重要项 5） |
| 加密 docx 的 `RuntimeError` 被吞成 500 | 中（v1 遗漏） | T1.3 显式 catch `RuntimeError` → `ValueError` → 400；Case 6b 验证 |
| 其余异常被 `except Exception` 吞成 500 | 中 | T1.3 统一转成 `ValueError`（`routes_splitter.py:94` 只认 ValueError → 400） |
| 超大 docx（20MB+） | 低 | 沿用现有 ThreadPoolExecutor 写盘路径；抽文本同步执行但仅一次 |
| 图片 / 文本框 / 页眉页脚内容丢失 | 低 | 明确已知限制：仅抽正文段落，正文外元信息不抽（对切章无损失） |
| 行尾符混乱 | 低 | 不批量转换任何现有文件；新增文件统一 LF（`core.autocrlf=true` 已在位，现有文件 100% CRLF） |

---

## 6. 已知限制（交付时需显式声明）

- 仅抽取 `word/document.xml` 的**正文段落**，`w:tbl`（表格）、页眉页脚、批注、脚注内容**不抽取**
- 不支持加密 / 受 DRM 保护的 DOCX（显式报错，不静默）
- 不支持旧二进制 `.doc`、`.rtf`、`.odt`
- DOCX 原始排版信息（字体 / 颜色 / 缩进）全部丢弃 —— 对切章场景无损失

---

## 7. 不做清单（避免范围蔓延）

- **EPUB**：差别复杂度显著更高（需处理 `opf:spine` 阅读顺序、单 xhtml 多章二次切分、DRM 三类问题），本次不做
- **`python-docx` 依赖**：理由见 D1
- **`.doc` 老格式**：二进制解析需 `olefile`，收益低
- **PDF 解析**：非结构化出身，与切章器假设不合
- **写 DOCX 导出**：无需求

---

## 8. 附带的一个硬约束提醒

项目 §9 硬性约束（已在 plan 执行时必须遵守）：

- ⚠️ **改完必须同步 `agent.md`**（§9.6）—— 本 plan 已把 T5 列为独立 Phase
- ❌ 禁止批量换行尾符（§9.2）
- ❌ 前端只改视觉层，不动 props / 事件 / 路由 / API / 数据结构（§9.5）
- 🔒 任何错误日志字段必须走 `redact()`（§9.1）—— 本改动若打印文件路径/异常需注意

## 9. 变更说明：本文件新增了什么

- 新增文件：`docs/PLAN-docx-input-2026-09-28.md`（即本文件）
- 未改动任何生产代码（本轮仅产出 plan）

---

## 10. 复审修订记录（v1 → v2）

v1 完成后已派 agent 做独立复审（只读，不改文件）。复审**确认了核心策略成立**：
`_detect_and_read` 确实是唯一入口（273 行，仅 preview_split:1067 / save_split:1101
两个调用方），三个 hook 点行号准确，443 用例数经 `--collect-only` 实测属实，
§0.2 的 `.txt` glob 论断无反例。**以下 5 项是复审挑出的实质问题，已在本版修复：**

| # | 严重度 | 问题 | v2 修法 |
|---|---|---|---|
| 1 | **阻塞** | A 验收要求 `word_count` 相等，但 D2 注入换行必然使其漂升 —— 断言自相矛盾，Phase 4 必然挂 | §1：从强一致中**移除** `word_count`，改为四元组 + `detected_pattern` 强一致，并注明 `word_count` 实为字符数（`len(body)`） |
| 2 | **阻塞** | D2「短行启发」的保守取向被判反：它截断正文而非产生空行，且会稀释 `lines[:800]` 采样窗口内的章节密度，可能让 `scores >= 5` 闸门选错正则 → **切出完全不同的结构** | §D2：**移除**短行启发，Phase 1 只做样式检测；新增 **Case 7** 断言两条路径 `detected_pattern` 相同 |
| 3 | **阻塞** | E 验收做不到：`split_text:835` 有显式 `if not raw_blocks` → 单章"全文" fallback，E 禁止的正是这个行为；而改它会破坏 C 验收的无回归 | §1.1：把 E **拆成 E1/E2**。E1（真空输入）由 T2.3 兜底抛错；**E2 显式不处理**，继承 TXT 既有行为并写明理由 |
| 4 | 重要 | T3.3 漏了第二处前端过滤 `SplitterPage.vue:197` 的 `accept=".txt"`；且 v1 写的 `file_types` 追加方式会产生嵌套元组 | T3.1 改为并列字符串写法；T3.3 补 197 行；新增 T3.4 UX 确认 |
| 5 | 重要 | T1.3 只 catch `BadZipFile`，但**加密 docx 抛的是 `RuntimeError`**；其余异常会被 `routes_splitter.py:94` 吞成 500 | T1.3 细化为四步分诊（magic bytes / BadZipFile / **RuntimeError** / testzip），统一转 `ValueError` → 400 |

**另采纳的次要项**：表格内正文改递归 `.iter(qn('w:p'))`（重要，网页转制 DOCX 常见）；
`word_count` 语义注释；`README` 多处而不仅两段；`architecture.md` 的 mermaid 节点；
新增 T4.6 记录根目录 `tests/` 不参与默认收集这一事实。

**复审结论原文**：「Nothing here challenges the plan's strategy — the single-entry-point
insight is correct and the surgical scope is right.」
即：**策略无需推翻，v2 已可按此开工。**
