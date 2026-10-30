# 验证记录（外置追加日志）

本文件是所有计划/报告文档的**验证记录索引**。外置存放的原因：若把验证记录写进被哈希的正文，写入哈希值会再次改变正文，形成自指，无法稳定。

---

## 规则

1. **只追加，不修改历史行。** 任何一行一旦写入，其 `scope`、`result`、`doc_sha256` 不得改动。
2. **两类验证分开记录**，不得合并成一个"通过"：
   - `doc_check`：文档层面的检查（章节完整、编号连续、覆盖表无遗漏、diff 可应用）
   - `business_test`：业务测试结果（pytest / vitest / 构建）
3. `doc_sha256` 对当时版本的**正文**计算，排除 frontmatter 与本文件引用。
4. 文档修订后新增一行，不覆盖旧行；旧行的结论**仅对该行的 hash 有效**。
5. 历史产物（patch、合入对照报告）保留原字节，通过本表索引标注其适用版本，不在文件内改写。

---

## 记录

| 日期 | commit | scope | 类型 | result | doc_sha256 | 说明 |
|---|---|---|---|---|---|---|
| 2026-10-30 | 尚未入库 | PLAN v2 合入（T01–T42） | doc_check | 通过 | 未记录 | 当时记录"42 项验收、diff 可逆"。该结论仅对当时的两个文档版本有效；后续对当前文档执行 `git apply --reverse --check` 已不能应用，属版本漂移，不推翻当时的记录 |
| 2026-10-30 | 尚未入库 | S6 补丁 v3 并入（T43–T44） | doc_check | 未重新验证 | 未记录 | 追加 T43/T44 与 S6 第 11、12 条。**未**对合入 patch 重新做可逆性验证，故本行 result 不含"通过" |
| 2026-10-30 | `3a27b6c` | 全量后端测试 | business_test | **864 passed / 92.17s** | — | 清理代理环境变量后实测。此为 v2 基线**之前**的历史数字，保留不改写 |
| 2026-10-30 | `5937cb1` | 全量后端测试（S0 后） | business_test | **892 collected / 566.84s** | — | S0 交付记录：原始未清理代理环境下 892 passed、5 warnings、exit 0。较 `3a27b6c` 增加 28 例（21 代理守卫 + 7 语料自检） |
| 2026-10-30 | `5937cb1` | 全量后端测试独立复现 | business_test | **892 collected / exit 0** | — | 另一会话独立复跑，未手动清代理，exit 0。耗时未记录 |
| 2026-10-30 | 尚未入库 | PLAN v2.2 复审二收口（scope 词汇统一 / 覆盖状态互斥 / 资源计量对象 / 附表 A 冻结口径 / T45·T47 自动化方式 / D-12 外置索引） | doc_check | 通过 | 未记录 | 主计划 703 行、delta 395 行、D-01 设计 257 行。校验：§5 内 T 唯一 47 条（T01–T47）无缺失无重复（附表 B 的 T 行是用例键，非重复定义）；两文档 `CALL/BLOCK/ACCOUNT` 旧词汇残留 0；`planned = executed` 旧守恒式残留 0 |
| 2026-10-30 | 尚未入库 | PLAN v2 S1 收口（U1–U6b：C-01 止血 + 判定贯通 + usage 语义） | doc_check | 通过 | 未记录 | `agent.md` 新增 §10.36 并改写 §5.1 的 `error_taxonomy` 三条 bullet；`CHANGELOG.md` 新增 S1 段落；`docs/architecture.md`「两级重试链」改为「两级重试链与错误判定」。行尾保真：CHANGELOG 纯 CRLF(722)、agent.md 纯 LF、architecture 原 CRLF 保持 |
| 2026-10-30 | 尚未入库 | 全量后端测试（S1 后） | business_test | **962 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，657.79s。相对 S0 基线 892 增加 70 例：新增 4 个测试文件 61 例（`test_error_types.py` 29 / `test_retry_decision_u2.py` 20 / `test_verdict_pipeline_u4b.py` 4 / `test_concurrency_and_usage_u6.py` 8）+ `test_error_taxonomy.py` 37→46（+9） |
| 2026-10-30 | 尚未入库 | 全量后端测试（S2 后） | business_test | **1022 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，1493.09s。相对 S1 基线 962 增加 60 例：`test_vnext_constants_alignment` 4 / `test_vnext_artifacts` 15 / `test_vnext_fingerprint` 15 / `test_vnext_project` 15 / `test_vnext_blocks` 11 |
| 2026-10-30 | 尚未入库 | PLAN v2 S2 文档同步 + 数据契约 | doc_check | 通过 | 未记录 | `agent.md` 新增 §10.37；`CHANGELOG.md` 新增 S2 段落；新建 `docs/data-contract-vnext.md`（385 行，LF 无 BOM，唯一 JSON 例子经 `json.loads` 校验）。行尾保真：CHANGELOG 纯 CRLF(753)、agent.md 纯 LF |

---

## 待补

- 上述两行 `doc_sha256` 为"未记录"。若需补，应基于**当时版本的文件字节**计算；不得用当前版本回填。

---

## 相关文件

| 文件 | 适用版本 | 状态 |
|---|---|---|
| `docs/PLAN-v2-long-novel-reliability-2026-10-30.md` | v2.2（T01–T47） | 活跃 |
| `docs/PLAN-S6-autonomous-resolution-2026-10-30.md` | v3 | 活跃 |
| `docs/REPORT-review-consolidated-2026-10-30.md` | 首版 | 活跃（待更正） |
| `docs/REVIEW-plan-v1-new-major-2026-10-30.md` | R1–R6 | 历史 |
| `docs/REVIEW-post-S0-plan-findings-2026-10-30.md` | R-1–R-7 | 现行复审 |
| `docs/PLAN-v2.2-delta-2026-10-30.md` | v2.2.1（含 §9 复审二增量） | 活跃 |
| `docs/patches/PLAN-v2-autonomous-resolution-2026-10-30.patch` | 对应 T01–T42 | **历史**，不适用于当前文档 |
| `docs/REPORT-plan-v2-autonomous-merge-2026-10-30.md` | 对应 T01–T42 | **历史**，记录 42 项 |
| `docs/BASELINE-2026-10-30.md` | S0 基线 | 活跃（待更正） |
