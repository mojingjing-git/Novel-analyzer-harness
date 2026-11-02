"""backend.common —— 1.x 与 vnext 共用的纯逻辑语义层。

纪律（由 backend/tests/test_architecture.py 守护）：

- 只依赖标准库与本包内部；禁止 import backend.core / backend.services / backend.vnext。
- 成员：错误分类体系（error_types / error_taxonomy）、审核标记契约（moderation）、
  LLM 错误信号嗅探（llm_signals）。
- core 侧同名模块（error_types / error_taxonomy / moderation）是兼容转发层，1.x 的
  引用面不变；新代码（尤其 vnext）直接从 backend.common 引用。
"""
