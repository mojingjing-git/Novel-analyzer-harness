"""
常量定义模块
集中管理所有硬编码的默认值、预设和范围限制
"""

# API默认配置（不预设具体模型：不同厂商模型迭代快，固定默认值易过时。
# 默认留空，要求用户在设置页/环境变量里明确填写自己要用的模型名）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_API_KEY = ""
DEFAULT_MODEL = ""  # 空 = 未配置模型，需用户在设置页填写

# 分析参数
DEFAULT_MAX_ARC_LENGTH = 1500      # 主线最大字数（长上下文建议800-1500）
DEFAULT_CONCURRENCY = 2            # 默认并行worker数
DEFAULT_BLOCK_SIZE = 1             # 默认每块章数（1=单章一块）

# Prompt构建参数（可配置）
DEFAULT_MAX_ARCS_IN_PROMPT = 30    # prompt中最多携带的主线条数
DEFAULT_MAX_SUMMARIES_IN_PROMPT = 5  # prompt中最多携带的摘要数
DEFAULT_TIMELINE_TRUNCATE = 200    # 时间线截断字符数
DEFAULT_MAX_CHARACTER_STATES_IN_PROMPT = 20
DEFAULT_MAX_WORLD_ITEMS_IN_PROMPT = 20
DEFAULT_MAX_FORESHADOW_ENTRIES_IN_PROMPT = 5
DEFAULT_BATCH_SUMMARY_MIN_WORDS = 2000  # 分卷摘要最低字数建议
DEFAULT_FINAL_REPORT_MIN_WORDS = 5000  # 最终报告最低字数建议

# 最终报告上下文预算保护：卷摘要拼接总字符数超过阈值时做分层压缩
DEFAULT_VOLUME_COMPRESS_THRESHOLD = 80000  # 卷摘要总字符数阈值（超过则触发压缩）
DEFAULT_VOLUME_COMPRESS_GROUP = 5          # 每 N 卷为一组（首尾各 1 组保留全文，中间组截断到 1/2）

# Rolling Summary 参数（结构化 JSON 增量方案）
ROLLING_SUMMARY_MAX_TOKENS = None      # rolling 摘要输出 token 上限（None=不限制，让模型自然结束）
ROLLING_SUMMARY_MAX_RETRIES = 1       # rolling 调用失败后最多重试次数（总调用 = 1 + 此值）
ROLLING_SUMMARY_TEMPERATURE = 0.2     # rolling 调用温度（低温保证稳定）
ROLLING_FILE_NAME = "rolling_summary.json"  # rolling 持久化文件名

# 结构化滚动总结参数
ROLLING_EARLY_CHAPTERS = 100          # 首次生成结构化 JSON 的触发章数
ROLLING_MAX_MILESTONES = 20           # 全局里程碑上限（FIFO淘汰最旧的）
ROLLING_MAX_MOMENTUM = 15             # 近期势头条目上限
ROLLING_MOMENTUM_WINDOW = 50          # 近期势头跨越多少章后触发归档
ROLLING_ARCHIVE_TRIGGER_COUNT = 10    # 条目数辅助触发归档（防单批爆满）

# 运行时参数
MAX_OUTPUT_TOKENS = 130000           # 单次LLM输出最大token数
DEFAULT_TIMEOUT = 300              # API超时时间（秒）—— 章节分析/单块调用
DEFAULT_SUMMARY_TIMEOUT = 600      # 最终总结 API 超时时间（秒）—— 最终报告输入更大（11 万字符级），需要更长超时；用于 final_summary 服务创建的所有 LLM 调用（卷摘要、最终报告、伏笔 reconciliation/复检、风格提取）
MAX_RETRIES = 5                    # 预留：APIConfig.max_retries 当前仅用于失败日志标注（record_failure 的 "尝试 n/m"），未接入重试循环；重试由 temperature_max_retries / backoff_max_retries 控制
SDK_MAX_RETRIES = 0                # SDK 底层 httpx 自动重试次数。设为 0，防止 SDK 隐式重试绕过上层重试控制与预算记账
DEFAULT_TEMPERATURE = 0.1          # 默认温度（温度退火起始值）
DEFAULT_TEMPERATURE_STEP = 0.05    # 温度退火每次降低量
# 注：本模块不定义 TEMPERATURE_SEQUENCE——该字段从未被重试逻辑
# 读取（温度退火实际使用 temperature_step 算术递减），属误导性死配置；
# config.json 中残留的 temperature_sequence 键会被忽略并提示删除。

# 重试配置
TEMPERATURE_MAX_RETRIES = 3        # 温度退火阶段最大重试次数
BACKOFF_MAX_RETRIES = 1            # 指数退避阶段默认重试次数

# 路径配置
OUTPUT_DIR_NAME = "output"

# 伏笔分类表 schema 版本号
# 调整 FORESHADOW_CATEGORY_DEFS 的内容/顺序时必须递增，触发 per-book type_map 自动失效
FORESHADOW_CATEGORY_SCHEMA_VERSION = 1

# 伏笔分类表（50 类，治本核心）
# 每条定义 = (name, description, example_anchors)
# - name: 中文短名（用于 catalog.category 字段 + UI 显示）
# - description: 一行定义（用于 LLM 归一化 prompt + UI tooltip）
# - example_anchors: 2-3 个典型 type 字符串示例（用作 LLM 判定的锚点，提升归一化准确率）
# 排序：人物→情节→冲突→关系→设定→主题→题材→兜底，按"功能"维度而非"题材"维度组织，
# 覆盖从严肃文学（伦理/存在/记忆）到网络小说（修炼/系统/爽点）的各种伏笔类型。
FORESHADOW_CATEGORY_DEFS = [
    # ===== 人物维度（8）=====
    ("身份", "身份揭示、面具、伪装、身份转换", ["身份伏笔", "身份揭示", "身份暗示"]),
    ("身世", "出身、血统、来源、家族背景揭示", ["身世伏笔", "身世之谜", "身世背景"]),
    ("性格", "性格特征、行为模式、性格转变", ["性格伏笔", "性格暗示", "性格发展"]),
    ("心理", "内心活动、动机、执念、潜意识", ["心理伏笔", "心理暗示", "心理活动"]),
    ("成长", "成长、改变、突破、觉醒", ["成长伏笔", "成长线索", "成长暗示"]),
    ("命运", "宿命、注定、轮回、因果", ["命运伏笔", "命运暗示", "宿命伏笔"]),
    ("关系", "人物关系变化、关系揭示、关系演变", ["关系伏笔", "关系暗示", "人际关系伏笔"]),
    ("情感", "爱情、友情、亲情、爱恨纠葛", ["情感伏笔", "感情伏笔", "爱情伏笔"]),

    # ===== 情节维度（10）=====
    ("战略", "战略布局、谋划、博弈、运筹", ["战略伏笔", "战略布局", "战略预示"]),
    ("阴谋", "阴谋、诡计、欺骗、暗算", ["阴谋伏笔", "阴谋线", "阴谋暗示"]),
    ("转折", "剧情转折、反转、意外、剧变", ["转折伏笔", "剧情反转", "意外伏笔"]),
    ("关键事件", "关键事件、节点、里程碑、转折点", ["关键事件", "重要事件", "节点伏笔"]),
    ("行动", "行动、决策、计划实施、举动", ["行动伏笔", "行动计划", "决策伏笔"]),
    ("悬疑", "谜题、未解之谜、线索、悬念", ["悬疑伏笔", "悬念", "未解之谜"]),
    ("伏线", "长期铺垫、贯穿全文、暗线", ["伏线", "长期伏笔", "暗线伏笔"]),
    ("日常", "日常生活、细节、氛围、生活片段", ["日常伏笔", "生活细节", "氛围伏笔"]),
    ("奇遇", "奇遇、机缘、巧合、奇缘", ["奇遇伏笔", "机缘伏笔", "巧合伏笔"]),
    ("爽点", "装逼、打脸、扬名、立威、扬眉吐气", ["爽点伏笔", "打脸伏笔", "装逼伏笔"]),

    # ===== 冲突维度（5）=====
    ("战斗", "战斗、武力、决斗、对决", ["战斗伏笔", "武力伏笔", "战斗暗示"]),
    ("危机", "危机、危险、灾难、紧迫形势", ["危机伏笔", "危险伏笔", "灾难伏笔"]),
    ("对抗", "对抗、冲突、矛盾、对峙", ["对抗伏笔", "冲突伏笔", "对峙伏笔"]),
    ("伤亡", "伤亡、死亡、牺牲、损失", ["伤亡伏笔", "死亡伏笔", "牺牲伏笔"]),
    ("复仇", "复仇、报仇、恩怨、宿怨", ["复仇伏笔", "报仇伏笔", "恩怨伏笔"]),

    # ===== 关系细分（3）=====
    ("敌友", "敌友关系、立场变化、阵营", ["敌友伏笔", "阵营伏笔", "立场伏笔"]),
    ("家族", "家族、血缘、宗族、传承", ["家族伏笔", "宗族伏笔", "血缘伏笔"]),
    ("师徒", "师徒、同门、门派传承", ["师徒伏笔", "同门伏笔", "门派传承"]),

    # ===== 设定维度（8）=====
    ("制度", "制度、法律、规则、礼法、典章", ["制度伏笔", "法律伏笔", "规则伏笔"]),
    ("势力", "势力、组织、阵营、门派、社团", ["势力伏笔", "阵营伏笔", "组织伏笔"]),
    ("世界设定", "世界观、规则体系、魔法/科技基础", ["世界设定", "世界观伏笔", "规则设定"]),
    ("历史背景", "历史、前史、传承、渊源", ["历史伏笔", "前史伏笔", "历史背景"]),
    ("文化", "文化、传统、风俗、信仰、宗教", ["文化伏笔", "传统伏笔", "信仰伏笔"]),
    ("资源", "资源、宝物、遗产、财产、神器", ["资源伏笔", "宝物伏笔", "遗产伏笔"]),
    ("修炼", "修炼、功法、境界、突破、异能等级", ["修炼伏笔", "功法伏笔", "境界伏笔"]),
    ("异能", "异能、特异功能、金手指、超能力", ["异能伏笔", "金手指伏笔", "超能力伏笔"]),

    # ===== 主题维度（6）=====
    ("伦理", "道德、伦理、选择、价值冲突", ["伦理伏笔", "道德伏笔", "价值冲突"]),
    ("存在", "存在主义、生命意义、生存危机", ["存在伏笔", "存在主义伏笔", "生命意义"]),
    ("时间", "时间、记忆、遗忘、过去未来", ["时间伏笔", "记忆伏笔", "时空伏笔"]),
    ("社会", "社会、阶层、权力结构、民俗", ["社会伏笔", "阶层伏笔", "社会结构"]),
    ("政治", "政治、权力、改革、朝堂、权力斗争", ["政治伏笔", "权力伏笔", "朝堂伏笔"]),
    ("战争", "战争、军事、战术、军队、战役", ["战争伏笔", "军事伏笔", "战术伏笔"]),

    # ===== 题材专用（10）=====
    ("穿越", "穿越、重生、转生、时空错位", ["穿越伏笔", "重生伏笔", "转生伏笔"]),
    ("系统", "系统、签到、面板、属性、任务", ["系统伏笔", "签到伏笔", "系统任务"]),
    ("升级", "升级、变强、进化、突破瓶颈", ["升级伏笔", "变强伏笔", "进化伏笔"]),
    ("商战", "商业、资本、竞争、并购", ["商战伏笔", "商业伏笔", "资本伏笔"]),
    ("职场", "职场、办公室、晋升、办公室政治", ["职场伏笔", "办公室伏笔", "晋升伏笔"]),
    ("推理", "案件、推理、调查、破案、真相", ["推理伏笔", "案件伏笔", "破案伏笔"]),
    ("科幻设定", "科技、AI、虚拟、星际、未来", ["科幻伏笔", "科技伏笔", "AI伏笔"]),
    ("奇幻设定", "魔法、种族、神话、神祇、异世界", ["奇幻伏笔", "魔法伏笔", "神话伏笔"]),
    ("医疗", "疾病、医疗、生死、康复、手术", ["医疗伏笔", "疾病伏笔", "手术伏笔"]),
    ("其他", "找不到合适类别时的兜底", []),
]

# 兜底类名（用于 LLM 归一化失败/幻觉类别回退）
FORESHADOW_CATEGORY_FALLBACK = "其他"

#: 模型自造的 type → 合法类名的别名表。
#:
#: 50 类是"源头约束"（提示词要求模型只从中选），但模型仍会自造复合词——
#: 真实长书实测：《大奉打更人》整本 63 种 type 里有 17 种不在词表内，
#: 涉及 20 条伏笔，全部落进兜底类「其他」。别名表把这类词收回正确的类，
#: 比逐本书手写映射更通用，也不必等有人去补 per-book 文件。
#:
#: 只收**语义明确**的词：拿不准的一律留给兜底，不要猜。
FORESHADOW_TYPE_ALIASES = {
    # —— 实测跑出来的（《大奉打更人》整本）——
    "身份揭示": "身份",
    "悬念": "悬疑",
    "能力限制": "异能",
    "修行": "修炼",
    "动机": "心理",
    "秘密": "悬疑",
    "修炼线索": "修炼",
    "秘密管控": "阴谋",
    "敌踪监视": "悬疑",
    "政敌牵制": "政治",
    "婚配安排": "情感",
    "成长环境": "成长",
    "行动计划": "行动",
    "冲突": "对抗",
    "规则": "制度",
    "权力": "政治",
    "伪装": "身份",
    # —— 常见同义说法 ——
    "身份之谜": "身份",
    "身世之谜": "身世",
    "身世秘密": "身世",
    "背景故事": "历史背景",
    "世界观": "世界设定",
    "设定": "世界设定",
    "暗线": "伏线",
    "埋伏笔": "伏线",
    "铺垫": "伏线",
    "线索": "悬疑",
    "谜题": "悬疑",
    "谜团": "悬疑",
    "未解之谜": "悬疑",
    "反转": "转折",
    "转折点": "转折",
    "打脸": "爽点",
    "装逼": "爽点",
    "扬名": "爽点",
    "功法": "修炼",
    "境界": "修炼",
    "突破": "修炼",
    "变强": "升级",
    "进化": "升级",
    "灵力": "异能",
    "法术": "奇幻设定",
    "魔法": "奇幻设定",
    "科技": "科幻设定",
    "系统流": "系统",
    "签到": "系统",
    "任务": "系统",
    "爱情": "情感",
    "友情": "情感",
    "亲情": "情感",
    "婚姻": "情感",
    "爱情线": "情感",
    "师徒情": "师徒",
    "同门": "师徒",
    "门派": "师徒",
    "家族秘辛": "家族",
    "宗族": "家族",
    "朝廷": "政治",
    "朝堂": "政治",
    "权谋": "政治",
    "夺嫡": "政治",
    "军情": "战争",
    "战役": "战争",
    "大军": "战争",
    "案件": "推理",
    "破案": "推理",
    "调查": "推理",
    "真相": "推理",
    "凶手": "推理",
    "商业": "商战",
    "资本": "商战",
    "职场斗争": "职场",
    "升职": "职场",
    "疾病": "医疗",
    "医治": "医疗",
    "康复": "医疗",
    "生死": "医疗",
    "日常细节": "日常",
    "生活": "日常",
    "巧合": "奇遇",
    "机缘": "奇遇",
    "奇缘": "奇遇",
    "危机降临": "危机",
    "危险": "危机",
    "死亡": "伤亡",
    "牺牲": "伤亡",
    "复仇": "复仇",
    "报仇": "复仇",
    "恩怨": "复仇",
    "敌友": "敌友",
    "阵营": "敌友",
    "立场变化": "敌友",
    "计谋": "战略",
    "谋划": "战略",
    "博弈": "战略",
    "诡计": "阴谋",
    "欺骗": "阴谋",
    "暗算": "阴谋",
    "关键节点": "关键事件",
    "重要事件": "关键事件",
    "性格": "性格",
    "性格变化": "性格",
    "转变": "性格",
    "成长线": "成长",
    "觉醒": "成长",
    "宿命": "命运",
    "因果": "命运",
    "轮回": "命运",
    "传统文化": "文化",
    "风俗": "文化",
    "宗教": "文化",
    "信仰": "文化",
    "宝物": "资源",
    "遗产": "资源",
    "财产": "资源",
    "神器": "资源",
    "道德": "伦理",
    "价值冲突": "伦理",
    "生存": "存在",
    "生命意义": "存在",
    "记忆": "时间",
    "遗忘": "时间",
    "过去": "时间",
    "未来": "时间",
    "阶层": "社会",
    "民俗": "社会",
    "重生": "穿越",
    "转生": "穿越",
    "时空错位": "穿越",
}


def normalize_foreshadow_type(ftype: str, valid_names=None) -> str | None:
    """把模型自造的 type 归一到 50 类词表里的合法类名。

    三步，**都拿不准就返回 ``None``**（由调用方落兜底类「其他」）：

    1. 精确命中合法类名 → 原样返回；
    2. 命中别名表 → 返回别名指向的类名；
    3. 在各类描述符里找**唯一**命中的词（如"冲突"只在"对抗"的描述里出现）
       → 返回该类。

    第 3 步要求唯一命中：像"权力"同时出现在"社会"与"政治"的描述里，
    猜错比落兜底更糟——兜底至少不会把伏笔归到一个错的类。
    """
    name = (ftype or "").strip()
    if not name:
        return None
    names = valid_names if valid_names is not None else {n for n, _d, _p in FORESHADOW_CATEGORY_DEFS}
    if name in names:
        return name
    alias = FORESHADOW_TYPE_ALIASES.get(name)
    if alias and alias in names:
        return alias
    hits = [
        cat for cat, desc, _p in FORESHADOW_CATEGORY_DEFS
        if cat in names and name in desc.split("、")
    ]
    if len(hits) == 1:
        return hits[0]
    return None

# 默认最低保留重要度
DEFAULT_FORESHADOW_MIN_IMPORTANCE = "中"
# 默认最低保留置信度（importance 已收一道，confidence 不再过度限制）
DEFAULT_FORESHADOW_MIN_CONFIDENCE = "低"
# 分层上限：importance=高 最多保留多少（-1=无上限）
DEFAULT_MAX_FORESHADOW_CATALOG_HIGH = 500
# 分层上限：importance=中 最多保留多少
DEFAULT_MAX_FORESHADOW_CATALOG_MID = 200

# per-book type→category 映射文件路径（相对于 book output_dir）。
# 主线为 prompt 源头约束（LLM 直接输出 50 类），此映射仅作为旧书数据的被动兜底。
FORESHADOW_TYPE_MAP_FILE = "foreshadow_type_map.json"

CONFIG_FILE_NAME = "config.json"
KNOWLEDGE_FILE_NAME = "knowledge.json"

# 编码尝试列表
ENCODING_CANDIDATES = [
    'utf-8', 'gbk', 'gb2312', 'gb18030', 'big5',
    'utf-16', 'utf-8-sig', 'latin-1', 'cp1252',
    'shift_jis', 'euc-kr'
]

# 知识库限制
MAX_COMPRESSED_ARCS = 50        # 最多保留50条压缩主线
MAX_RECENT_SUMMARIES = 5        # 保留最近5章摘要
MAX_PACING_TRACKER = 100        # pacing_tracker 最大条目
MAX_FORESIGHT_NETWORK = 100     # foreshadowing_network 最大条目
MAX_WORLD_BUILDING = 200        # world_building 最大条目
MAX_VERIFIED_FACTS = 200        # verified_facts 最大条目
MAX_LONG_TERM_ARCS = 200        # long_term_arcs 最大条目
MAX_THEMATIC_ELEMENTS = 100     # thematic_elements 最大条目
MAX_KB_CHARACTER_STATES = 200   # character_states 持久化上限（按最近活跃保留）
MAX_KB_CHARACTER_RELATIONSHIPS = 300  # character_relationships 持久化上限（按最近活跃保留）
# i18n 接口预留：仅作为 future i18n 的接口字段
# 当前不影响任何 UI 行为；未来 i18n 启用时此字段驱动 vue-i18n locale 选择
DEFAULT_LANGUAGE = "zh-CN"
# 注：未来启用语言切换 UI 时再补 SUPPORTED_LANGUAGES = ("zh-CN", "en")

# 内存优化：Checkpoint 间隔（批数），每 N 批自动落盘，0=只在结束时落盘
DEFAULT_CHECKPOINT_INTERVAL = 5

# 全书语义图谱与风格画像产物路径（放在 constants 供路由/viz/final_summary 共享）
STORY_ATLAS_FILE = "story_atlas.json"
STORY_ATLAS_SCHEMA_VERSION = 1
STYLE_PROFILE_CHECKPOINT_REL = "final_summary_checkpoint/style_profile.json"
