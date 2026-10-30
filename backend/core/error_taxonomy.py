"""
厂商错误码分类体系（error_taxonomy）

把"按字符串猜"的分散错误判定收敛为统一分类器：审核拦截 / 计费 / 限流 / 上下文超长 /
认证 / 参数错的确定性识别与重试决策。设计见 docs/PLAN-provider-error-taxonomy-2026-09-30.md
（D1/D2 为 2026-10-25 修订版，并入 §8 P2-1/3/4 与 §9 P2-5/P3-6/8/9/10/11）。

两个入口（来源分级见下"来源分级"）：
- classify_exception(e)：异常对象在场时的结构化通道（异常属性码 / body 深挖 / HTTP 状态兜底），
  无命中返回 None，调用方用 classify_message 兜底
- classify_message(msg)：字符串通道，永不返回 None（默认 UNKNOWN/none）——只认命名错误码
  与错误措辞，不认裸数字；结论的 action 上限是可重试，永不 stop

判定优先级（P2-4 写死）：
  MODERATION_INPUT → MODERATION_OUTPUT → BILLING → CONTEXT_LENGTH → AUTH →
  BAD_REQUEST → RATE_LIMIT → SERVER → NETWORK → TIMEOUT → UNKNOWN
通道优先级：body 码表 > HTTP 状态兜底（structured）> 命名码/文本措辞（text）> UNKNOWN（none）。
前插规则（先于一切）：百炼 Throttling.* 前缀强制 RATE_LIMIT（insufficient_quota 在百炼是限流语义）。

审核判侧（P2-5）：码表逐条带侧；双侧码默认 MODERATION_OUTPUT（保守，保留 3 次短路）。

来源分级（C-01 的核心契约，2026-10-30）：
  - structured：SDK 异常属性 / 错误 envelope / 真实 HTTP 状态码——唯一有资格产出
    "永久停止"（RETRY_POLICY=stop）的来源
  - text：字符串通道里的命名码（insufficient_quota 等）与错误措辞——只给 cls 与证据，
    action 一律不得为 stop（最多 limited_retry）；是否停止由调用方按预算与轮数决定
  - none：没有任何可信信号——UNKNOWN + 有限重试（limited_retry）

已知接受的误报面（实现备注）：
- 纯数字厂商码（1026/1027/1008/1113/1039/1261/20012 等）**只**在 structured 通道生效，
  文本通道不做数字扫描：正文里的"第 401 章""赚了 1008 块""总字数 1026 万"曾被判成
  AUTH/BILLING/MODERATION_INPUT 并永久熔断（C-01）
- 文本通道里的 HTTP 状态码必须带 "HTTP"/"Error code" 前缀，裸数字（如 "第 401 章"）不参与判定
"""

import re
from collections import namedtuple
from typing import Dict, List, Optional

# `ErrorClass` 的定义处是 `error_types`（最底层叶子模块），本模块只 re-export，
# 避免两份同值枚举导致 `is` 比较静默为 False（两份 str-Enum 取值相同但身份不同，
# `==` 为真而 `is` 为假，跨模块判定会被无声地判错）。
# 依赖方向：error_taxonomy → error_types（单向，不构成循环导入）。
from .error_types import ErrorClass  # noqa: F401  （re-export：调用方从这里取 ErrorClass）


# 重试策略（§1.4）。MODERATION_* 的"何时停"（INPUT 首次 / structured 首次 / text 连续 3 次）
# 由调用方短路计数器表达，不塞进 policy（§8 P3-2）——这里 INPUT=stop 仅表示"不再重试"。
RETRY_POLICY: Dict[ErrorClass, str] = {
    ErrorClass.AUTH: "stop",
    ErrorClass.BILLING: "stop",
    ErrorClass.CONTEXT_LENGTH: "stop",
    ErrorClass.BAD_REQUEST: "stop",
    ErrorClass.MODERATION_INPUT: "stop",
    ErrorClass.MODERATION_OUTPUT: "stop",
    ErrorClass.RATE_LIMIT: "retry_after",
    ErrorClass.SERVER: "backoff",
    ErrorClass.NETWORK: "backoff",
    ErrorClass.TIMEOUT: "backoff",
    ErrorClass.UNKNOWN: "backoff",
}


# ---------------------------------------------------------------------------
# 来源分级：置信度与 action（C-01）
#    action 由唯一策略函数 resolve_action 计算后固化在 ClassVerdict 里，调用方只消费，
#    不再自行推导 cls → action（与 error_types.ErrorVerdict.action 的语义一致）
# ---------------------------------------------------------------------------

CONF_STRUCTURED = "structured"  # 可信通道：SDK 异常属性 / 错误 envelope / 真实 HTTP 状态码
CONF_TEXT = "text"              # 文本兜底：命名错误码或错误措辞
CONF_NONE = "none"              # 没有任何可信信号

ACTION_STOP = "stop"                    # 只有 structured 通道能给出
ACTION_RETRY_AFTER = "retry_after"      # 按 Retry-After 重试
ACTION_BACKOFF = "backoff"              # 指数退避重试
ACTION_LIMITED_RETRY = "limited_retry"  # 有限重试（文本兜底/未知的上限，轮数由调用方预算决定）

# 文本通道的 action 上限：不在此表的一律降级为 ACTION_LIMITED_RETRY（永不为 stop）
_TEXT_ACTION: Dict[ErrorClass, str] = {
    ErrorClass.RATE_LIMIT: ACTION_RETRY_AFTER,
    ErrorClass.SERVER: ACTION_BACKOFF,
    ErrorClass.NETWORK: ACTION_BACKOFF,
    ErrorClass.TIMEOUT: ACTION_BACKOFF,
}


def resolve_action(cls: ErrorClass, confidence: str = CONF_TEXT) -> str:
    """按来源通道计算 action（唯一策略入口）。

    - structured：可信来源，直接取 RETRY_POLICY（可以为 stop）
    - text：文本兜底，**永不为 stop**——限流仍 retry_after、瞬态仍 backoff，
      其余（认证/计费/审核/参数错/上下文超长）一律降级为 limited_retry
    - none：无信息，limited_retry（有限重试，不一次判死也不无限退避）

    RETRY_POLICY 因此保留为"可信通道的策略表"；文本通道的上限在此处施加，
    调用方若想用文本结论做永久熔断，必须显式绕开本函数（C-01 的防线在此）。
    """
    if confidence == CONF_STRUCTURED:
        return RETRY_POLICY.get(cls, ACTION_BACKOFF)
    if confidence == CONF_TEXT:
        return _TEXT_ACTION.get(cls, ACTION_LIMITED_RETRY)
    return ACTION_LIMITED_RETRY


_ClassVerdictBase = namedtuple("ClassVerdict", ("cls", "confidence", "action"))


class ClassVerdict(_ClassVerdictBase):
    """一次分类结论（不可变三元组：cls / confidence / action）

    - cls：ErrorClass
    - confidence：CONF_STRUCTURED / CONF_TEXT / CONF_NONE
      （与 error_types.Confidence 的 STRUCTURED/TEXT/NONE 用 .upper() 一一对应）
    - action：构造时不显式传入则按 resolve_action 自动推导，于是"文本通道不得 stop"
      由类型本身保证，而不是靠调用方记得检查

    与旧二元组兼容：cls/confidence 的位置与属性名不变，action 追加在尾部。
    """

    __slots__ = ()

    def __new__(cls, cls_: ErrorClass, confidence: str = CONF_NONE,
                action: Optional[str] = None):
        if action is None:
            action = resolve_action(cls_, confidence)
        return super().__new__(cls, cls_, confidence, action)

    def is_permanent(self) -> bool:
        """是否"永久停止"结论：必须同时是可信通道 + stop 类。

        这是给调用方（llm_client / pipeline）的唯一合法判据：文本通道的结论恒为 False，
        因此纯文本永远不能登记 fatal（C-01）。下一阶段把它们从 RETRY_POLICY[cls] 改到
        本方法即完成收口。
        """
        return self.confidence == CONF_STRUCTURED and self.action == ACTION_STOP


_MOD_CLASSES = (ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT)

# 跨类目判定优先级（P2-4）。文本通道同样按此序取第一个命中，但命中结果一律 text 置信、
# action 受 resolve_action 上限约束（永不为 stop），避免"正文里的数字决定熔断"（C-01）
_PRIORITY: List[ErrorClass] = [
    ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT, ErrorClass.BILLING,
    ErrorClass.CONTEXT_LENGTH, ErrorClass.AUTH, ErrorClass.BAD_REQUEST,
    ErrorClass.RATE_LIMIT, ErrorClass.SERVER, ErrorClass.NETWORK, ErrorClass.TIMEOUT,
]


def _norm(code) -> Optional[str]:
    """码值归一化：str → strip → lower；非标量返回 None"""
    if isinstance(code, bool) or not isinstance(code, (str, int)):
        return None
    s = str(code).strip().lower()
    return s or None


# ---------------------------------------------------------------------------
# 1) 结构化码表（body/异常属性通道，归一化后精确匹配；前缀族单独处理）
#    来源标注见 docs/PLAN §1.1-1.3 调研（各厂商官方文档）
# ---------------------------------------------------------------------------

BODY_CODE_MAP: Dict[str, ErrorClass] = {
    # --- 内容审核（侧标注 P2-5；both→OUTPUT）---
    "1026": ErrorClass.MODERATION_INPUT,    # MiniMax base_resp：输入涉敏
    "1027": ErrorClass.MODERATION_OUTPUT,   # MiniMax base_resp：输出涉敏
    "1301": ErrorClass.MODERATION_OUTPUT,   # 智谱：输入或生成内容不安全（双侧）
    "datainspectionfailed": ErrorClass.MODERATION_OUTPUT,   # 百炼（双侧）
    "data_inspection_failed": ErrorClass.MODERATION_OUTPUT,
    "content_policy_violation": ErrorClass.MODERATION_OUTPUT,  # OpenAI/OpenRouter（双侧）
    "content_filter": ErrorClass.MODERATION_OUTPUT,            # OpenAI/Kimi（双侧）
    "moderation_blocked": ErrorClass.MODERATION_OUTPUT,        # OpenAI 图像
    "refusal": ErrorClass.MODERATION_OUTPUT,                   # OpenRouter typed（仅 body 语境，P3-6）
    # 旧 moderation.MODERATION_CODES 裸词码吸收（仅 body 通道，防消息误报，P3-6）
    "safety": ErrorClass.MODERATION_OUTPUT,
    "moderation": ErrorClass.MODERATION_OUTPUT,
    "policy_violation": ErrorClass.MODERATION_OUTPUT,
    "unsupported_content": ErrorClass.MODERATION_OUTPUT,
    "inappropriate_content": ErrorClass.MODERATION_OUTPUT,
    "harmful_content": ErrorClass.MODERATION_OUTPUT,
    # --- 计费（"按状态码必错"陷阱的落地：429/403/400 伪装全靠码表先行）---
    "insufficient_balance_error": ErrorClass.BILLING,   # MiniMax Anthropic 皮肤 402
    "1008": ErrorClass.BILLING,                          # MiniMax base_resp
    "insufficient_quota": ErrorClass.BILLING,            # OpenAI 429 伪装（百炼限流语义由 Throttling 前插覆盖）
    "credit_balance_exhausted": ErrorClass.BILLING,      # OpenAI
    "billing_hard_limit_reached": ErrorClass.BILLING,   # OpenAI
    "exceeded_current_quota_error": ErrorClass.BILLING,  # Kimi 429 伪装
    "1113": ErrorClass.BILLING,                          # 智谱（HTTP 429 伪装）
    "accountoverdueerror": ErrorClass.BILLING,           # 火山方舟 403 伪装
    "arrearage": ErrorClass.BILLING,                     # 百炼 400 伪装
    "billing_error": ErrorClass.BILLING,                 # Anthropic 402
    # --- 上下文超长 ---
    "context_length_exceeded": ErrorClass.CONTEXT_LENGTH,  # OpenAI/OpenRouter/DeepSeek
    "1039": ErrorClass.CONTEXT_LENGTH,                     # MiniMax
    "1261": ErrorClass.CONTEXT_LENGTH,                     # 智谱
    "request_too_large": ErrorClass.CONTEXT_LENGTH,        # 413 语义归此
    # --- 认证（401 族；AccessDenied 不入表：方舟 403 语境实为欠费，由 BILLING 码优先命中）---
    "invalid_api_key": ErrorClass.AUTH,          # OpenAI
    "authentication_error": ErrorClass.AUTH,     # Anthropic/OpenAI
    "invalid_authentication_error": ErrorClass.AUTH,  # Anthropic
    "authenticationerror": ErrorClass.AUTH,      # 火山方舟
    "invalidapikey": ErrorClass.AUTH,            # 百炼
    "1000": ErrorClass.AUTH,                     # 智谱 body-only（"1000" 太易误报，不进消息扫描）
    "1001": ErrorClass.AUTH,                     # 智谱 body-only
    "1003": ErrorClass.AUTH,                     # 智谱 body-only
    "1005": ErrorClass.AUTH,                     # 智谱 body-only
    # --- 参数/模型错误（确定性，不自愈）---
    "modelnotfound": ErrorClass.BAD_REQUEST,     # 火山方舟
    "model_not_found": ErrorClass.BAD_REQUEST,
    "20012": ErrorClass.BAD_REQUEST,             # 硅基流动（官方语义=模型不存在）
    "1211": ErrorClass.BAD_REQUEST,              # 智谱 body-only（模型不存在）
    "invalid_parameter": ErrorClass.BAD_REQUEST,  # 火山方舟
    "invalidparameter": ErrorClass.BAD_REQUEST,
    "missingparameter": ErrorClass.BAD_REQUEST,   # 火山方舟
    "2013": ErrorClass.BAD_REQUEST,               # MiniMax body-only
    # 真 Anthropic 的 unprocessable_entity_error 是参数错；MiniMax 皮肤 422 输入拦截由条件规则升格（P2-1）
    "unprocessable_entity_error": ErrorClass.BAD_REQUEST,
    # --- 限流 ---
    "rate_limit_error": ErrorClass.RATE_LIMIT,
    "rate_limit_exceeded": ErrorClass.RATE_LIMIT,
    "1302": ErrorClass.RATE_LIMIT,   # 智谱 body-only
    "1308": ErrorClass.RATE_LIMIT,   # 智谱 body-only
    "1310": ErrorClass.RATE_LIMIT,   # 智谱 body-only
}

# 前缀族（body 与消息两通道共用；先于类优先序查表）
_PREFIX_RULES = [
    ("sensitivecontentdetected", ErrorClass.MODERATION_INPUT),  # 方舟，覆盖 .SevereViolation
    ("throttling.", ErrorClass.RATE_LIMIT),                     # 百炼限流族
]

# HTTP 状态兜底（仅 body 码全空时；400/404/422 不兜底——未识别的 400 保守按 UNKNOWN 退避）
_STATUS_FALLBACK: Dict[int, ErrorClass] = {
    401: ErrorClass.AUTH,
    403: ErrorClass.AUTH,
    402: ErrorClass.BILLING,
    413: ErrorClass.CONTEXT_LENGTH,
    429: ErrorClass.RATE_LIMIT,
}
# 5xx → SERVER（含 Anthropic 529 overloaded）用范围判断


# ---------------------------------------------------------------------------
# 2) 消息码串扫描白名单（classify_message 的命名码通道；P3-6 第二套表）
#    只收"命名的"错误码：snake/camel 码按子串匹配，命中一律 text 置信。
#    **纯数字厂商码不进本表**（C-01，2026-10-30）：数字只有在结构化字段（异常属性 / body）
#    里才可信；此前扫描任意文本里的数字，正文的"第 401 章 / 赚了 1008 块 / 总字数 1026 万"
#    被判成 AUTH / BILLING / MODERATION_INPUT 并永久熔断。
#    排除：refusal、裸词码（safety/moderation…）、智谱 1000/1302 系、1211/2013/1301
# ---------------------------------------------------------------------------

_MESSAGE_SCAN_CODES: Dict[str, ErrorClass] = {
    "sensitivecontentdetected": ErrorClass.MODERATION_INPUT,
    "datainspectionfailed": ErrorClass.MODERATION_OUTPUT,
    "data_inspection_failed": ErrorClass.MODERATION_OUTPUT,
    "content_policy_violation": ErrorClass.MODERATION_OUTPUT,
    "content_filter": ErrorClass.MODERATION_OUTPUT,
    "moderation_blocked": ErrorClass.MODERATION_OUTPUT,
    "insufficient_balance_error": ErrorClass.BILLING,
    "insufficient_quota": ErrorClass.BILLING,
    "credit_balance_exhausted": ErrorClass.BILLING,
    "billing_hard_limit_reached": ErrorClass.BILLING,
    "exceeded_current_quota_error": ErrorClass.BILLING,
    "accountoverdueerror": ErrorClass.BILLING,
    "arrearage": ErrorClass.BILLING,
    "billing_error": ErrorClass.BILLING,
    "context_length_exceeded": ErrorClass.CONTEXT_LENGTH,
    "request_too_large": ErrorClass.CONTEXT_LENGTH,
    "invalid_api_key": ErrorClass.AUTH,
    "authentication_error": ErrorClass.AUTH,
    "invalid_authentication_error": ErrorClass.AUTH,
    "invalidapikey": ErrorClass.AUTH,
    "model_not_found": ErrorClass.BAD_REQUEST,
    "modelnotfound": ErrorClass.BAD_REQUEST,
    "invalid_parameter": ErrorClass.BAD_REQUEST,
    "rate_limit_error": ErrorClass.RATE_LIMIT,
    "rate_limit_exceeded": ErrorClass.RATE_LIMIT,
}


def _compile_scan(code: str) -> re.Pattern:
    """编译码串匹配式。纯数字码只可能出现在 structured 码表（BODY_CODE_MAP），
    本通道按约定不含数字码，`isdigit` 分支仅作防御性保留（见测试里的表内容守护）。"""
    if code.isdigit():
        return re.compile(rf"\b{code}\b", re.IGNORECASE)
    return re.compile(re.escape(code), re.IGNORECASE)


_MESSAGE_SCAN_RES: Dict[ErrorClass, List[re.Pattern]] = {c: [] for c in _PRIORITY}
for _code, _cls in _MESSAGE_SCAN_CODES.items():
    _MESSAGE_SCAN_RES[_cls].append(_compile_scan(_code))


# ---------------------------------------------------------------------------
# 3) 文本兜底正则（classify_message 的 text 通道；审核表 = 旧 moderation.py 全量保留）
# ---------------------------------------------------------------------------

MODERATION_TEXT_PATTERNS: List[str] = [
    # 中文（旧表原样迁移）
    r"安全规范", r"内容审核", r"内容安全", r"敏感", r"违规", r"不合规",
    r"不符合.{0,6}规范", r"被过滤", r"拒绝回答", r"涉敏", r"高风险",
    r"风险内容", r"无法生成.{0,8}内容", r"涉及.{0,6}(色情|暴力|政治)",
    # 英文（旧表原样迁移）
    r"high risk", r"safety policy", r"moderation", r"sensitive",
    r"content filter", r"filtered", r"prohibited", r"blocked",
    r"violat", r"inappropriat", r"harmful", r"unsafe", r"unacceptable",
    # 新增：[MODERATION] 标记本身（生成器已标记的串直接命中）
    r"\[moderation\]",
    # 新增：MiniMax new_sensitive 族（"output new_sensitive (1027)" 等）
    r"new_sensitive",
    # 新增：content policy 短语（gold fixture §4：OpenAI 系 400 拦截缩略串）
    r"content policy",
]

# 输入侧升级（P2-1/P2-5）：命中则 MODERATION_INPUT（确定性输入拦截），否则 OUTPUT
_MOD_INPUT_RE = re.compile(
    r"input.{0,12}sensitive|input_sensitive|输入内容涉敏", re.IGNORECASE)

# 文本正则（classify_message 的 text 通道）。数字只在带状态码前缀时参与判定——
# "HTTP 401" / "Error code: 500" 是错误措辞，而裸数字（"第 401 章"）不是（C-01）
_TEXT_PATTERNS: Dict[ErrorClass, List[str]] = {
    ErrorClass.BILLING: [
        r"余额不足", r"欠费", r"insufficient balance", r"out of balance",
        r"credit balance", r"insufficient credits", r"充值",
    ],
    ErrorClass.CONTEXT_LENGTH: [
        r"context length", r"context_length", r"input length", r"prompt is too long",
        r"prompt.{0,16}long", r"maximum context", r"上下文长度",
    ],
    ErrorClass.AUTH: [
        r"认证失败", r"(?:http|error code:?) ?401\b", r"invalid api[ _-]?key",
        r"incorrect api[ _-]?key", r"unauthorized", r"鉴权失败",
    ],
    ErrorClass.BAD_REQUEST: [
        # 窄表：仅确定性参数错，防止误伤可重试错误
        r"must provide a model", r"model (does not exist|not found|不存在)",
        r"模型不存在", r"不存在的模型", r"invalid model",
    ],
    ErrorClass.RATE_LIMIT: [
        r"(?:http|error code:?) ?429\b", r"rate.?limit", r"too many requests", r"throttling",
        r"用量上限", r"usage limit", r"触发限流", r"速率限制", r"频率限制",
    ],
    ErrorClass.SERVER: [
        r"error code: ?5\d\d", r"http ?5\d\d",
        r"overloaded", r"bad gateway", r"service unavailable",
        r"internal server error", r"服务器繁忙", r"服务器错误",
    ],
    ErrorClass.NETWORK: [
        r"connection (error|reset|refused|closed|aborted)", r"econn\w*", r"reset by peer",
        r"\bssl\b", r"getaddrinfo", r"连接失败", r"网络(错误|异常|中断)",
    ],
    ErrorClass.TIMEOUT: [
        r"硬超时", r"\btime ?out\b", r"timed out", r"请求超时", r"api 无响应",
    ],
}
_TEXT_RES: Dict[ErrorClass, re.Pattern] = {
    cls: re.compile("|".join(pats), re.IGNORECASE)
    for cls, pats in _TEXT_PATTERNS.items()
}
_MOD_TEXT_RE = re.compile("|".join(MODERATION_TEXT_PATTERNS), re.IGNORECASE)

# 前插规则（先于类优先序）
_THROTTLING_RE = re.compile(r"throttling[.:]", re.IGNORECASE)
# 422 条件规则（P2-1）：MiniMax Anthropic 皮肤的输入审核拦截 vs 真 Anthropic 参数错。
# 只认命名码 unprocessable_entity_error，不再要求文本里出现裸数字 "422"（C-01）
_UNPROCESSABLE_RE = re.compile(r"unprocessable_entity_error", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 分类入口
# ---------------------------------------------------------------------------

def _mine_body_codes(e) -> List[str]:
    """从异常 body 深挖候选码（OpenAI 风格 error.code/type、MiniMax base_resp、顶层 code/type）。
    非 dict body（字符串/数组等聚合网关怪格式）防御性跳过——对齐 moderation_hit_from_exception
    的 P1 修复语义（2026-08-24）。"""
    body = getattr(e, "body", None)
    if not isinstance(body, dict):
        return []
    codes: List[str] = []
    err_inner = body.get("error")
    if isinstance(err_inner, dict):
        for key in ("code", "type"):
            c = _norm(err_inner.get(key))
            if c:
                codes.append(c)
    for key in ("code", "type"):  # 部分网关把码放顶层
        c = _norm(body.get(key))
        if c:
            codes.append(c)
    base_resp = body.get("base_resp")
    if isinstance(base_resp, dict):  # MiniMax 原生
        c = _norm(base_resp.get("status_code"))
        if c:
            codes.append(c)
    return codes


def classify_exception(e) -> Optional[ClassVerdict]:
    """异常对象在场时的结构化通道（可信来源）。只查结构化信号，无命中返回 None
    （调用方 `classify_exception(e) or classify_message(msg)` 兜底）。

    返回的结论 confidence=structured，action 取 RETRY_POLICY（可以是 stop）——
    这是全系统唯一能产出"永久停止"的通道。"""
    codes: List[str] = []
    for attr in ("code", "type"):  # openai-python 从 body 解析的一等属性；anthropic e.type
        c = _norm(getattr(e, attr, None))
        if c:
            codes.append(c)
    codes.extend(_mine_body_codes(e))
    status = getattr(e, "status_code", None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None

    # 422 条件规则（P2-1）：先于码表（unprocessable_entity_error 裸表值是 BAD_REQUEST）
    if status == 422 and any(c == "unprocessable_entity_error" for c in codes):
        msg_text = ""
        body = getattr(e, "body", None)
        if isinstance(body, dict):
            err_inner = body.get("error")
            if isinstance(err_inner, dict):
                msg_text = str(err_inner.get("message") or "")
        if _MOD_TEXT_RE.search(msg_text) or _MOD_TEXT_RE.search(str(e)):
            return ClassVerdict(ErrorClass.MODERATION_INPUT, CONF_STRUCTURED)

    # 前缀族
    for c in codes:
        for prefix, cls in _PREFIX_RULES:
            if c.startswith(prefix):
                return ClassVerdict(cls, CONF_STRUCTURED)

    # 类优先序查码表
    for cls in _PRIORITY:
        for c in codes:
            if BODY_CODE_MAP.get(c) is cls:
                return ClassVerdict(cls, CONF_STRUCTURED)

    # HTTP 状态兜底
    if status is not None:
        fb = _STATUS_FALLBACK.get(status)
        if fb is None and 500 <= status <= 599:
            fb = ErrorClass.SERVER
        if fb is not None:
            return ClassVerdict(fb, CONF_STRUCTURED)
    return None


def classify_message(msg) -> ClassVerdict:
    """字符串通道（重试循环/补跑过滤用）。永不返回 None。

    证据上限：只认"命名的"错误码与错误措辞，命中记 text 置信；毫无信号时给 UNKNOWN/none。
    本通道的结论**永不产生 action=stop**（由 resolve_action 兜底），所以纯文本不能成为
    永久熔断的依据——是否停止交给调用方按预算与轮数决定（C-01，2026-10-30）。
    """
    text = str(msg or "")
    if not text.strip():
        return ClassVerdict(ErrorClass.UNKNOWN, CONF_NONE)

    # 前插：百炼 Throttling.*（命名前缀，覆盖 insufficient_quota 同名冲突）
    if _THROTTLING_RE.search(text):
        return ClassVerdict(ErrorClass.RATE_LIMIT, CONF_TEXT)

    # 422 条件规则的消息形态（P2-1）：日志串 "UnprocessableEntityError: {...input new_sensitive...}"
    if _UNPROCESSABLE_RE.search(text):
        if _MOD_TEXT_RE.search(text):
            return ClassVerdict(ErrorClass.MODERATION_INPUT, CONF_TEXT)
        return ClassVerdict(ErrorClass.BAD_REQUEST, CONF_TEXT)

    # 命名码串扫描（text 置信，按类优先序；表内不含纯数字码）
    for cls in _PRIORITY:
        for rx in _MESSAGE_SCAN_RES[cls]:
            if rx.search(text):
                return ClassVerdict(cls, CONF_TEXT)

    # 文本正则（text 置信，按类优先序；审核命中后再判侧）
    if _MOD_TEXT_RE.search(text):
        side = (ErrorClass.MODERATION_INPUT if _MOD_INPUT_RE.search(text)
                else ErrorClass.MODERATION_OUTPUT)
        return ClassVerdict(side, CONF_TEXT)
    for cls in _PRIORITY:
        if cls in _MOD_CLASSES:
            continue
        rx = _TEXT_RES.get(cls)
        if rx and rx.search(text):
            return ClassVerdict(cls, CONF_TEXT)
    return ClassVerdict(ErrorClass.UNKNOWN, CONF_NONE)


# ---------------------------------------------------------------------------
# moderation.py 兼容转发面（T1.2；契约函数留在 moderation.py，判定逻辑在此）
# ---------------------------------------------------------------------------

MODERATION_BODY_CODES: Dict[str, ErrorClass] = {
    code: cls for code, cls in BODY_CODE_MAP.items() if cls in _MOD_CLASSES
}


def is_moderation_code(code) -> bool:
    """结构化错误码判定（code 可为 str/int/None；兼容旧 MODERATION_CODES 语义——精确匹配）"""
    c = _norm(code)
    return c is not None and c in MODERATION_BODY_CODES


def is_moderation_message(message) -> bool:
    """自由文本关键词判定（旧表全量保留 + 标记 + new_sensitive）"""
    if not message:
        return False
    return bool(_MOD_TEXT_RE.search(str(message)))
