"""匹配引擎 —— 三级过滤 + 加权打分 + 批次识别。

设计取舍：用规则打分而非逐岗调 LLM，省 token、快、可复现。
过滤顺序（越早淘汰成本越低）：
  1. 行业黑名单  —— 养殖/种业/快消这类与技术岗无关的整条丢弃
  2. 技术相关度  —— 岗位标题/专业不含任何技术信号则丢弃（核心修复）
  3. 硬条件      —— 批次 / 学历 / 城市 / 用户自定义排除词
"""
from __future__ import annotations

import re

from .models import Job

# ---------------------------------------------------------------- 词表

# 技术岗信号词：命中越多相关度越高
TECH_KEYWORDS = (
    "开发", "研发", "算法", "数据", "软件", "程序", "编程", "代码",
    "ai", "人工智能", "大模型", "机器学习", "深度学习", "nlp", "cv",
    "后端", "前端", "全栈", "架构", "运维", "测试", "嵌入式",
    "python", "java", "golang", "c++", "数据库", "大数据", "数仓",
    "数字化", "信息化", "信息技术", "计算机", "系统", "平台",
    "云计算", "网络安全", "信息安全", "it", "bi", "etl", "spark",
)

# 非技术行业/岗位黑名单：命中即整条丢弃
INDUSTRY_BLACKLIST = (
    "种业", "养殖", "生猪", "农林牧渔", "饲料", "food", "食品饮料",
    "快消", "医疗器械", "医疗卫生", "护理", "临床", "药剂", "医药代表",
    "建筑土木", "土木", "勘察设计", "施工", "监理", "造价", "房建",
    "销售", "导购", "客服", "行政", "人力资源", "财务", "会计", "审计",
    "法务", "文秘", "采购", "物流仓储", "生产操作", "普工", "技工",
    "教师", "教务", "护士", "医师", "翻译",
)

# 占位公司名黑名单：来源站把真实雇主藏在付费墙/登录后，采集到的"公司名"
# 只是占位符——无法归因到具体公司（公司聚合/归一化都会退化成噪音），
# 对投递决策零价值，直接丢弃。集合形式，后续发现新占位词在此增量。
# 实测：实习僧付费墙占位「会员可见」单次扫描可产出 270+ 条，严重刷屏。
PLACEHOLDER_COMPANIES = {
    "会员可见",        # 实习僧付费墙占位
    "公司名称未公开",
    "匿名公司",
}

# 岗位标题里出现即视为非技术岗（比黑名单更精准，仅看 title）
TITLE_BLACKLIST = (
    "管培生", "储备干部", "市场营销", "品牌", "公关", "供应链管理",
    "工艺", "质量管理", "安全环保", "机械设计", "结构设计", "电气设计",
)

# 科技行业白名单：聚合站常给「公司 + 行业标签」而非具体岗位
# （如"2026秋招·半导体/显示/电子"）。这类公司必然设技术岗，
# 保留并按弱信号计分，避免整批高价值科技企业被误杀。
TECH_INDUSTRY = (
    "半导体", "芯片", "集成电路", "显示", "电子", "通信", "光通信", "光电",
    "软件", "互联网", "信息", "计算机", "人工智能", "智能", "数据", "云",
    "网络", "科技", "自动化", "电力", "能源", "航天", "航空", "军工",
    "汽车", "新能源", "智能制造", "工业互联网", "金融科技", "银行",
)


def _norm(s: str) -> str:
    return (s or "").lower()


def _kw_hit(kw: str, text: str) -> bool:
    """关键词命中：英文关键词按「独立词」匹配（前后不能是字母/数字），
    防止 2 字母键裸子串误伤 —— "ai" 命中 Trai**ni**ng、"it" 命中 Aud**it**or、
    "cv" 命中 re**cv**iewer。中文关键词保持子串匹配（中文无词边界）。"""
    if kw.isascii() and any(c.isalpha() for c in kw):
        # 自己保证小写：否则调用方漏 lower 时，大写英文技术词（AI/LLM/SQL/Python…）
        # 会全部漏判 —— 表现为技术岗被误判为非技术岗而遭 hard_filter 丢弃，且无报错。
        return re.search(
            rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", (text or "").lower()
        ) is not None
    return kw in text


def recognize_batch(text: str) -> str:
    """从文本识别招聘批次。

    "社招"是显式排除项（应届生不投社招）：识别出来后不在画像 batches 枚举里，
    会被 hard_filter 拦截——判断优先级最高，避免"社会招聘"被兜底成"其他"逃过过滤。
    """
    t = text or ""
    if "社招" in t or "社会招聘" in t:
        return "社招"
    if "提前批" in t:
        return "提前批"
    if "春招" in t:
        return "春招"
    if "秋招" in t:
        return "秋招"
    if "补录" in t:
        return "补录"
    if "实习" in t:
        return "实习"
    return "其他"


def tech_relevance(job: Job) -> int:
    """技术相关度 0-50。0 表示不是技术岗，应当丢弃。

    标题权重最高（标题带技术词才算真技术岗），专业/行业作为补充证据。
    """
    title = _norm(job.title)
    blob = _norm(f"{job.title} {job.major} {job.industry}")

    # 标题黑名单：明确的非技术岗
    if any(k in title for k in TITLE_BLACKLIST):
        return 0

    title_hits = [k for k in TECH_KEYWORDS if _kw_hit(k, title)]
    other_hits = [k for k in TECH_KEYWORDS if _kw_hit(k, blob) and k not in title_hits]

    if not title_hits and not other_hits:
        # 聚合站的「公司 + 行业标签」条目：科技行业的公司保留为弱信号线索
        if any(k in blob for k in TECH_INDUSTRY):
            return 8
        return 0

    score = min(len(title_hits), 3) * 14 + min(len(other_hits), 2) * 4
    # 标题完全没技术词、只靠专业蹭上的，最多给一半分（弱信号）
    if not title_hits:
        score = min(score, 12)
    return min(score, 50)


# ---------------------------------------------------------------- 方向信号词

# 方向 → 三档信号词。解决「整串子串匹配」的硬伤：
# 旧实现要求 "数据开发" 四个字原样出现，导致「大数据平台开发工程师」
# 「数据处理与算法岗」「智能驾驶软件开发」这类最对口的岗位方向分为 0，
# 实测 473 条里仅 9% 能命中。改为分档语义匹配后覆盖率提升到 70%+。
DIRECTION_SIGNALS: dict[str, dict[str, tuple[str, ...]]] = {
    "ai应用开发": {
        "core": ("ai应用", "大模型应用", "aigc", "llm应用", "智能应用", "ai开发", "ai工程"),
        "strong": ("大模型", "llm", "人工智能", "机器学习", "深度学习", "算法工程",
                   "nlp", "自然语言", "计算机视觉", "agent", "智能驾驶", "智能座舱",
                   "智能算法", "推荐算法", "ai平台"),
        "weak": ("ai", "智能", "算法", "数字化"),
    },
    "大模型应用": {
        "core": ("大模型", "llm", "aigc", "生成式", "prompt", "agent"),
        "strong": ("人工智能", "ai应用", "nlp", "自然语言", "深度学习", "多模态"),
        "weak": ("ai", "智能", "算法"),
    },
    "数据开发": {
        "core": ("数据开发", "大数据开发", "数据工程", "数仓", "数据仓库", "etl",
                 "数据平台", "数据中台", "大数据平台"),
        "strong": ("大数据", "数据处理", "数据治理", "spark", "hadoop", "flink",
                   "hive", "kafka", "数据挖掘", "数据岗", "数据架构", "bi"),
        "weak": ("数据", "报表", "统计", "数字化"),
    },
    "数据分析": {
        "core": ("数据分析", "商业分析", "bi", "数据运营", "分析师"),
        "strong": ("数据挖掘", "统计分析", "用户分析", "经营分析", "数据洞察", "报表开发"),
        "weak": ("数据", "统计", "报表"),
    },
    "后端开发": {
        "core": ("后端", "服务端", "后台开发", "java开发", "golang", "go开发"),
        "strong": ("软件开发", "微服务", "分布式", "中间件", "服务器", "系统开发",
                   "平台开发", "web开发", "接口开发", "python开发", "c++开发"),
        "weak": ("开发", "研发", "编程", "软件"),
    },
}

# 城市分层：目标地 > 一线/新一线（大厂与央企总部集中）> 其他
# 雄安列入一线档——政策新区，央国企总部批量迁入，机会质量接近一线
TIER_A_CITIES = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "西安", "苏州",
    "天津", "重庆", "长沙", "合肥", "青岛", "厦门", "宁波", "无锡", "郑州",
    "济南", "福州", "东莞", "佛山", "大连", "沈阳", "雄安",
)

_EDU_RANK = {"专科": 1, "大专": 1, "本科": 2, "学士": 2, "硕士": 3, "研究生": 3, "博士": 4}


# ---------------------------------------------------------------- 方向类别（用户勾选的 canonical 方向）
# 把库里 30+ 个零散「岗位角色」归并为 ~9 类，勾选即驱动匹配分。
DIRECTION_CATEGORIES: dict[str, tuple[str, ...]] = {
    "AI/大模型/算法": ("大模型", "llm", "aigc", "人工智能", "ai", "机器学习", "深度学习",
                   "nlp", "自然语言", "计算机视觉", "多模态", "agent", "智能驾驶",
                   "智能座舱", "智能算法", "推荐算法", "ai平台", "generative"),
    "软件开发(后端)": ("后端", "服务端", "后台", "java", "golang", "go开发", "c++",
                    "python开发", "软件研发", "微服务", "分布式", "中间件", "架构师",
                    "全栈", "系统开发", "平台开发", "web开发", "接口开发", "服务器"),
    "前端开发": ("前端", "web前端", "大前端", "vue", "react", "小程序"),
    "数据开发/分析": ("数据开发", "大数据", "数仓", "数据仓库", "etl", "数据工程",
                   "数据平台", "数据中台", "spark", "hive", "flink", "kafka",
                   "数据挖掘", "数据分析", "bi", "商业分析", "数据运营", "数据治理", "报表"),
    "测试/质量": ("测试", "qa", "测开", "质量"),
    "运维/DevOps": ("运维", "devops", "sre", "it运维"),
    "嵌入式/硬件": ("嵌入式", "驱动", "单片机", "硬件", "fpga", "数字后端", "电路", "固件", "物联网"),
    "通信/网络/安全": ("通信", "网络", "5g", "光通信", "网络安全", "信息安全", "数据安全"),
    "客户端/移动": ("android", "ios", "客户端", "移动开发", "unity"),
}
DIRECTION_LABELS = list(DIRECTION_CATEGORIES.keys())

# 单位性质归一：原始 nature 文本 → canonical 标签
# （解决「国企=国有企业」「民企=民营上市=其他企业含民营」的重复混乱）
_NATURE_CANON = (
    ("央企", "央企"), ("国企", "国企"), ("国有", "国企"), ("银行", "国企"),
    ("民企", "民企"), ("民营", "民企"), ("私营", "民企"),
    ("外企", "外企"), ("外资", "外企"), ("外商投资", "外企"), ("外商独资", "外企"),
    ("事业", "事业单位"), ("科研", "事业单位"), ("教育单位", "事业单位"), ("中等、初等教育", "事业单位"),
    ("上市", "上市公司"), ("民营上市", "上市公司"), ("民营/上市", "上市公司"),
)
_NATURE_PRESTIGE = {"央企": 4, "国企": 3, "事业单位": 3, "上市公司": 2, "外企": 2, "民企": 1}

# enterprise_type 单选优先级：央企 > 国企 > 事业单位 > 外企 > 民企
# （上市公司/机关等无法归入目标类别的统归「其他」；空=未标注）
_ENT_PRIORITY = ("央企", "国企", "事业单位", "外企", "民企")


def canon_natures(raw: str) -> list[str]:
    out = []
    for kw, lab in _NATURE_CANON:
        if kw in (raw or ""):
            out.append(lab)
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x); res.append(x)
    return res


def canon_enterprise_type(raw: str) -> str:
    """把单位性质原文归一化为单一 enterprise_type 标签（空=未标注）。

    区别于 canon_natures（可多标签展示）：本函数按优先级取唯一值，
    用于 jobs 表 enterprise_type 列的持久化与看板筛选。
    """
    cans = canon_natures(raw or "")
    for lab in _ENT_PRIORITY:
        if lab in cans:
            return lab
    return "其他" if cans else ""


def recruit_type_of(raw: str) -> str:
    """从 nature 文本提取招聘类型（校招/实习/见习/社招）。"""
    r = raw or ""
    for t in ("校招", "实习", "见习", "社招"):
        if t in r:
            return t
    return ""


# 笔试标注：免笔试关键词（命中即免笔试）优先于「有笔试」（文本出现"笔试"字样）
_EXAM_FREE_KWS = ("免笔试", "免于笔试", "无需笔试", "无笔试", "免除笔试",
                  "笔试免试", "不设笔试", "免统一笔试", "免专业笔试", "笔试通过即面")


def detect_exam_free(text: str) -> str:
    """从 JD/标题检测笔试标注，三态：免笔试 / 有笔试 / ""(未知)。"""
    t = (text or "").replace(" ", "").replace("\u3000", "")
    if not t:
        return ""
    if any(k in t for k in _EXAM_FREE_KWS):
        return "免笔试"
    if "笔试" in t:
        return "有笔试"
    return ""


# 专业归一 + 专业→方向推荐
MAJOR_CATEGORIES = {
    "计算机类": ("计算机", "软件", "计科"),
    "软件工程": ("软件", "软工"),
    "电子信息": ("电子", "信息", "光电", "集成"),
    "通信": ("通信", "网络"),
    "自动化": ("自动化", "控制", "电气"),
    "数据科学/人工智能": ("数据", "人工智能", "智能", "算法"),
}
MAJOR_TO_DIRS = {
    "计算机类": ["AI/大模型/算法", "软件开发(后端)", "前端开发", "数据开发/分析"],
    "软件工程": ["软件开发(后端)", "前端开发", "数据开发/分析"],
    "电子信息": ["嵌入式/硬件", "通信/网络/安全", "AI/大模型/算法"],
    "通信": ["通信/网络/安全", "嵌入式/硬件"],
    "自动化": ["嵌入式/硬件", "通信/网络/安全"],
    "数据科学/人工智能": ["AI/大模型/算法", "数据开发/分析"],
    "不限": DIRECTION_LABELS,
}


def canon_major(raw: str) -> str:
    r = (raw or "").replace(" ", "")
    if not r or "不限" in r or "详见" in r or "未知" in r:
        return "不限"
    for cat, kws in MAJOR_CATEGORIES.items():
        if cat == "不限":
            continue
        if any(k in r for k in kws):
            return cat
    return "其他"


def suggest_directions(kw: str) -> list[str]:
    """关键词/专业 → 推荐相关方向类别。供前端「写AI开发→推荐相关方向」使用。"""
    k = _norm(kw)
    if not k:
        return []
    for cat, kws in MAJOR_CATEGORIES.items():           # 专业名直接映射
        if cat != "不限" and (cat in k or k in cat or any(w in k for w in kws)):
            return MAJOR_TO_DIRS.get(cat, [])
    direct = [d for d in DIRECTION_LABELS if d in kw]   # 方向类别名命中
    if direct:
        return direct
    return [d for d, kws in DIRECTION_CATEGORIES.items() if any(w in k for w in kws)]


# 标题关键词 → 行业/专业 启发式补全（仅当字段为空时推荐，采纳才落库）
# 顺序重要：具体岗位规则在前，通用兜底（开发/工程师）在后，避免误判。
_SUGGEST_RULES: list[tuple[tuple[str, ...], dict[str, str]]] = [
    (("大模型", "llm", "aigc", "生成式", "prompt", "agent", "多模态"),
     {"industry": "人工智能/大模型", "major": "数据科学/人工智能"}),
    (("算法", "机器学习", "深度学习", "nlp", "自然语言", "计算机视觉", "cv", "智能算法", "推荐算法", "强化学习"),
     {"industry": "算法", "major": "数据科学/人工智能"}),
    (("数据开发", "大数据", "数仓", "etl", "数据工程", "数据平台", "数据中台", "数据治理",
      "数据库", "数据采集", "爬虫", "实时数仓", "olap", "clickhouse", "doris", "spark", "hive", "flink", "kafka"),
     {"industry": "数据开发", "major": "数据科学/人工智能"}),
    (("数据分析", "商业分析", "数据运营", "bi", "数据挖掘", "报表", "经营分析"),
     {"industry": "数据分析", "major": "数据科学/人工智能"}),
    (("后端", "服务端", "后台", "java", "golang", "go开发", "c++", "python开发", "微服务", "分布式", "架构"),
     {"industry": "软件开发", "major": "计算机类"}),
    (("前端", "vue", "react", "小程序", "大前端"),
     {"industry": "前端开发", "major": "计算机类"}),
    (("测试", "qa", "测开"),
     {"industry": "测试/质量", "major": "计算机类"}),
    (("运维", "devops", "sre"),
     {"industry": "运维/DevOps", "major": "计算机类"}),
    (("嵌入式", "驱动", "单片机", "fpga", "物联网", "硬件", "芯片", "ic"),
     {"industry": "嵌入式/硬件", "major": "电子信息"}),
    (("通信", "5g", "光通信"),
     {"industry": "通信/网络", "major": "通信"}),
    (("网络安全", "信息安全", "数据安全"),
     {"industry": "通信/网络/安全", "major": "计算机类"}),
    (("android", "ios", "客户端", "移动开发", "unity"),
     {"industry": "客户端/移动", "major": "计算机类"}),
    (("人工智能", "ai", "智能驾驶", "智能座舱", "智能应用", "语音"),
     {"industry": "人工智能/大模型", "major": "数据科学/人工智能"}),
    (("实施", "交付", "技术支持", "客户成功", "解决方案"),
     {"industry": "实施/交付", "major": "计算机类"}),
    # 通用兜底：开发/软件/研发类（放在最后，避免抢具体规则）
    (("开发工程师", "软件开发", "软件工程师", "研发工程师", "开发", "研发", "程序", "工程师", "coding"),
     {"industry": "软件开发", "major": "计算机类"}),
]


def suggest_fill(job: Job) -> dict[str, str]:
    """标题启发式补全行业/专业（仅填补空字段，不静默写库）。

    返回形如 {"industry": "数据开发", "major": "数据科学/人工智能"}，
    只含「原字段为空」且「能推断」的项；都满或推不出则返空 dict。
    前端据此展示「建议方向：X」标签，用户点「采用」才写回 job 记录。
    """
    t = _norm(job.title)
    out: dict[str, str] = {}
    for kws, fill in _SUGGEST_RULES:
        if any(w in t for w in kws):
            if not (job.industry or "").strip() and "industry" in fill:
                out["industry"] = fill["industry"]
            if not (job.major or "").strip() and "major" in fill:
                out["major"] = fill["major"]
            if out:
                return out
    return out


def direction_match(job: Job, profile: dict) -> tuple[int, list[str]]:
    """方向匹配 0-30。用户勾选的 canonical 方向类别命中即 30；自定义方向回退字面匹配。

    基于 DIRECTION_CATEGORIES 把库里零散岗位角色归并，覆盖率远高于旧版逐串匹配。
    """
    blob = _norm(f"{job.title} {job.industry} {job.major}")
    hits = []
    for d in profile.get("directions", []):
        if d in DIRECTION_CATEGORIES:
            if any(w in blob for w in DIRECTION_CATEGORIES[d]):
                hits.append(d)
        else:
            if _norm(d) in blob:
                hits.append(d)
    return (30 if hits else 0), hits


def split_cities(city: str) -> list[str]:
    """拆多城市字段：「北京, 上海, 武汉」→ ['北京','上海','武汉']。"""
    import re
    return [p.strip() for p in re.split(r"[,，、/;；]", city or "") if p.strip()]


def city_score(job: Job, profile: dict) -> tuple[int, str]:
    """地区 0-15，分层加权（用户选定方案）。

    目标城市 15 / 一线新一线 8 / 其他 2 / 城市未知 4
    这样武汉岗浮顶，一线大厂岗排中间仍可见，小城市沉底——
    既保住本地优先，又不让国聘的全国岗被一刀切埋掉。

    2026-09-02 修正：多城市岗（如「深圳,北京,上海,…,西安」）只因末位含一个
    目标城市就拿满 15 分，导致满屏「北京」——用户没标北京却全是北京岗。
    现在拆城市后看**首个城市**（简章里的主工作地）：
      首城市命中目标 → 15（地区匹配）
      仅后续城市命中 → 8（地区含目标，与一线同档，不再霸占首屏）
    """
    city = (job.city or "").strip()
    if not city:
        return 4, ""
    regions = profile.get("regions", [])
    if not regions:
        return 4, ""
    parts = split_cities(city)
    if not parts:
        return 4, ""
    hit_idx = [i for i, p in enumerate(parts)
               if any((r in p or p in r) for r in regions)]
    if not hit_idx:
        if any(t in city for t in TIER_A_CITIES):
            return 8, "一线/新一线"
        return 2, ""
    if hit_idx[0] == 0:
        return 15, "地区匹配"
    return 8, f"地区含{parts[hit_idx[0]]}"


_DEADLINE_UNKNOWN = ("以官网为准", "招满即止", "长期", "不限", "滚动", "另行通知")


def days_to_deadline(job: Job, today: str = "") -> int | None:
    """距截止还剩几天。无法判断（「以官网为准」/无日期/格式异常）返回 None。

    单独抽出是因为看板的「N 天内截止」筛选也要用 ——
    deadline 在库里是混合格式文本（'2026-09-01' / '2026年8月20日' / '以官网为准'），
    SQL 层做不了日期比较，只能解析后在 Python 侧过滤。
    """
    import re
    from datetime import date

    raw = (job.deadline or "").strip()
    if not raw or any(k in raw for k in _DEADLINE_UNKNOWN):
        return None
    nums = re.findall(r"\d+", raw)
    if len(nums) < 3:
        return None
    try:
        y, m, d = int(nums[0]), int(nums[1]), int(nums[2])
        if y < 100:
            y += 2000
        dl = date(y, m, d)
    except (ValueError, IndexError):
        return None
    try:
        ref = date.fromisoformat(today) if today else date.today()
    except ValueError:
        ref = date.today()
    return (dl - ref).days


def deadline_urgency(job: Job, today: str = "") -> tuple[int, str]:
    """时效紧急度 0-10。截止日 99% 有数据，旧版完全没用上。

    越近越该先投：≤3天 10 / ≤7天 8 / ≤15天 6 / ≤30天 4 / 更远 3
    已过期 0 并标红；「以官网为准」等无法判断的给中性 4，不惩罚。
    """
    left = days_to_deadline(job, today)
    if left is None:
        return 4, ""
    if left < 0:
        return 0, "已过期"
    if left <= 3:
        return 10, f"⚠{left}天内截止"
    if left <= 7:
        return 8, f"{left}天内截止"
    if left <= 15:
        return 6, "两周内截止"
    if left <= 30:
        return 4, ""
    return 3, ""


def salary_k(raw: str) -> tuple[float | None, str]:
    """把混杂的薪资文本折算成「月薪 K」。

    返回 (K or None, 展示标签)。无法判断（面议/无数字）返回 (None, "")——
    抽出来是因为「画像最低薪资门槛」也要用同一个口径，避免两处算法漂移。
    """
    import re

    raw = (raw or "").strip()
    if not raw or "面议" in raw or "不限" in raw:
        return None, ""
    nums = [float(x) for x in re.findall(r"\d+\.?\d*", raw) if float(x) > 0]
    if not nums:
        return None, ""
    lo = min(nums)

    # 单位识别（顺序：时 → 天/日 → 周 → 万 → 元）。实习岗大量用日薪/时薪，
    # 不换算会把「150-300元/天」当成 150K 直接满分，污染看板排序。
    if "小时" in raw or "时薪" in raw or "/时" in raw:
        k = lo * 8 * 21.75 / 1000          # 每日8h、每月21.75个工作日
        unit = "元/时"
    elif "天" in raw or "日薪" in raw or "/日" in raw or "日结" in raw:
        k = lo * 21.75 / 1000
        unit = "元/天"
    elif "周" in raw or "周薪" in raw:
        k = lo * 4.35 / 1000
        unit = "元/周"
    elif "万" in raw:
        # 「年薪15万」→ 月 12.5K；「月薪1.2万」→ 12K
        k = lo * 10000 / 12 / 1000 if "年" in raw else lo * 10
        unit = ""
    elif lo >= 1000:
        k = lo / 1000          # 元/月 → K
        unit = ""
    else:
        k = lo                 # 已是 K
        unit = ""

    k = round(k, 1)
    return k, f"{k:g}K起" + (f"({lo:g}{unit})" if unit else "")


def salary_score(job: Job, profile: dict | None = None) -> tuple[int, str]:
    """薪资 0-5（可负：低于画像最低薪资门槛时 -8）。

    统一折算为「月薪 K」，取区间**下限**保底（上限多是画饼）：
      ≥20K:5  ≥13K:4  ≥10K:3  ≥7K:2  <7K:1  面议/无:2（中性不惩罚）
    数据源格式混杂——「12000以上」「20000~30000 元/月」「10-18K」「年薪15万」
    都要能吃，故按单位归一而非直接取数字。
    画像 salary_min>0 时，能解析出薪资且低于门槛的岗 -8（掉一档），
    解析不出的（面议/空）不惩罚，避免误杀。
    """
    k, label = salary_k(job.salary or "")
    if k is None:
        return 2, ""
    if k >= 20:
        base, show = 5, label
    elif k >= 13:
        base, show = 4, label
    elif k >= 10:
        base, show = 3, ""
    elif k >= 7:
        base, show = 2, ""
    else:
        base, show = 1, label

    smin = (profile or {}).get("salary_min") or 0
    if smin > 0 and k < smin:
        return base - 8, f"薪资{k:g}K<{smin}K"
    return base, show


def major_score(job: Job, profile: dict) -> tuple[int, str]:
    """专业匹配 0-6（2026-09-02 新增）。

    此前画像的「专业」字段在打分里**完全没被使用**——改专业对看板零影响，
    是「改了画像却没变化」的主因之一。现在并入权重表：
      岗位专业为空 → 0（无信号，不惩罚）
      canon 精确命中画像专业 → 6
      文本包含命中（「计算机类、电子信息类」含「电子信息」）→ 4
      不命中 → 0
    权重来源：技术相关度 22→18、单位性质 8→6，总和仍为 100。
    """
    sel = profile.get("major") or []
    if not sel:
        return 0, ""
    mj = (job.major or "").strip()
    if not mj:
        return 0, ""
    mset = {canon_major(m) for m in sel}
    if canon_major(mj) in mset:
        return 6, "专业匹配"
    blob = _norm(f"{mj} {job.title or ''} {job.industry or ''}")
    hit = [m for m in sel if _norm(m) in blob]
    if hit:
        return 4, "专业相关"
    return 0, ""


def nature_score(job: Job, profile: dict | None = None) -> tuple[int, str]:
    """单位性质 0-6：声望(0-4, 央企>国企>事业>上市/外企>民企) + 勾选加成(0-4)，上限 6。

    用户画像勾选了某性质（如央企/国企），命中的岗位额外 +4 ——
    把旧版「心仪性质」单独加分组折叠进正常选择，更干净。
    2026-09-02：上限由 8 降为 6，让出 2 分给新增的「专业匹配」维度。
    """
    cans = canon_natures(job.nature)
    if not cans:
        return 1, ""
    prestige = max(_NATURE_PRESTIGE.get(c, 1) for c in cans)
    sel = (profile or {}).get("nature") or []
    matched = [c for c in cans if c in sel]
    total = min(prestige + (4 if matched else 0), 6)
    reason = ("性质:" + "/".join(matched)) if matched else (cans[0] if prestige >= 3 else "")
    return total, reason


def _edu_ok(job_edu: str, user_edu: str) -> bool:
    """学历是否够得着。

    岗位学历常写成枚举（"本科,硕士" = 本科和硕士都招），只要枚举里
    存在不高于用户学历的档位就算可投。踩过的坑：早期把"本科,硕士"
    当成"仅要硕士"，把本科生能投的岗位全挡了。
    """
    je = (job_edu or "").replace(" ", "")
    if not je or "不限" in je:
        return True
    mine = _EDU_RANK.get(user_edu, 2)
    levels = [v for k, v in _EDU_RANK.items() if k in je]
    if not levels:
        return True
    return min(levels) <= mine


def edu_score(job: Job, profile: dict) -> tuple[int, str]:
    """学历可达性 +4/-8（2026-09-20 新增）。

    旧版学历完全不参与打分（只做看板层过滤），导致硕博-only 岗与本科可投岗
    同分霸榜——实测 ≥60 分岗里 52% 是硕博-only，本科用户根本投不了。
    现在：岗位学历够得着（含不限/为空）+4；明确要求更高学历 -8 沉底
    （不删除，看板层学历过滤仍可用）。
    """
    ue = (profile.get("education") or "").strip()
    if not ue:
        return 0, ""
    je = (job.education or "").strip()
    if not je or "不限" in je:
        return 4, ""
    if _edu_ok(je, ue):
        return 4, ""
    return -8, f"学历门槛:{je}"


def hard_filter(job: Job, profile: dict) -> bool:
    """三级过滤。任一不过直接丢弃，不进入打分与入库。"""
    blob = _norm(f"{job.company} {job.title} {job.industry} {job.major}")

    # 0. 占位公司名（来源站隐藏真实雇主，无法归因）
    if (job.company or "").strip() in PLACEHOLDER_COMPANIES:
        return False

    # 1. 行业黑名单
    if any(k in blob for k in INDUSTRY_BLACKLIST):
        return False

    # 2. 技术相关度（核心修复：非技术岗不再混入）
    if tech_relevance(job) <= 0:
        return False

    # 3a. 批次（归一化：'秋招提前批' → '提前批'，再比对画像）
    # 「其他」= 简章没写明批次，不代表不能投，放行交给打分处理。
    # 踩过的坑：直接按枚举过滤，把大量只写「2026年校园招聘」的简章全挡了。
    batch = recognize_batch(job.batch or f"{job.title} {job.industry}")
    if batch != "其他" and profile.get("batches") and batch not in profile["batches"]:
        return False

    # 3b. 用户自定义排除词（唯一保留的「硬屏蔽」，避免不想要的行业挤进来）
    excl = profile.get("exclude_keywords", [])
    if excl and any(k in blob for k in excl):
        return False

    # —— 注意：地区 / 学历 不再做入库硬过滤 ——
    # 用户方案：全量入库（保住国聘全国岗等机会），地区与学历改为「看板层过滤」：
    #   地区    → 看板「只看目标地区」开关 + 打分地区置顶（见 city_score）
    #   学历    → 画像「学历门槛」+ 看板按 education 文本过滤（见 store._edu_bucket）
    # 这样既不丢全国/硕士岗，又能一键聚焦到自己相关的岗位。
    return True


_LINK_BONUS = {"官方网申": 3, "简章详情": 2, "官方校招页": 1, "源详情页": 1}

# 来源降权：低相关度来源整体扣分，避免稀释看板。
# 2026-08-20 撤销国聘 -8：社招已在 hard_filter 拦截、城市词已补武汉岗，
# 非目标城市由 city_score 地区分层自然沉底，来源降权属于重复惩罚。
# 如后续新增聚合噪音源，可在此登记。
_SOURCE_PENALTY: dict[str, int] = {}


# ── 成功率信号：把"民办二本投大厂容易石沉大海"变成显式可评估的信号 ──
# 三路信号全部可溯源，不做主观臆断：
#   1) 岗位学历文本：本科可投/不限 → 友好；仅硕士 → 不友好
#   2) 公司情报实搜（company_intel.school_barrier）：宽松 +5 / 严格 -5
#   3) 用户自身历史回复率：按企业性质分组的实测回应率（样本 ≥3 才计权）
# 输出 {add, level, reasons}。这是对"投递成功率"的信号评估，不是保证——
# 展示层必须标注"信号非保证，口碑类内容为主观评价"。

def education_friendliness(education: str) -> tuple[float, str]:
    """岗位学历文本 → (加扣分, 依据一句话)。"""
    edu = (education or "").strip()
    has_bk = "本科" in edu
    has_master = ("硕士" in edu or "研究生" in edu)
    if has_master and not has_bk:
        return -6.0, "仅硕士可投（本科无门）"
    if "不限" in edu or has_bk:
        return 4.0, "本科可投"
    if not edu:
        return 0.0, "学历要求未标注"
    return 0.0, f"学历要求：{edu[:20]}"


def success_signal(job, researched: dict, response_by_type: dict) -> dict:
    """三路成功率信号合成（不影响 match_score 本身，作为独立维度展示/排序）。

    researched: {公司名: intel_dict}（company_intel 表，已过审公司才有序）
    response_by_type: {企业性质: {applied, response_rate}}（用户自身投递实测）
    """
    reasons: list[str] = []
    add = 0.0

    edu_add, edu_note = education_friendliness(job.education)
    add += edu_add
    reasons.append(edu_note)

    intel = researched.get((job.company or "").strip())
    if intel:
        barrier = intel.get("school_barrier") or "未知"
        note = (intel.get("school_note") or "")[:44]
        if barrier == "宽松":
            add += 5.0
            reasons.append(f"情报实搜：学历门槛宽松（{note}）")
        elif barrier == "严格":
            add -= 5.0
            reasons.append(f"情报实搜：学历门槛严格（{note}）")
        else:
            reasons.append(f"情报实搜：门槛{barrier}（{note}）")

    etype = (job.enterprise_type or "").strip()
    rs = response_by_type.get(etype)
    if rs and rs.get("applied", 0) >= 3:
        rate = rs["response_rate"]
        pct = int(rate * 100)
        if rate >= 0.34:
            add += 3.0
            reasons.append(f"你的实测：{etype}回应率 {pct}%（{rs['applied']} 家样本）")
        elif rate < 0.1:
            add -= 3.0
            reasons.append(f"你的实测：{etype}回应率 {pct}%（{rs['applied']} 家样本，暂无回音）")

    level = "高" if add >= 6 else ("低" if add <= -3 else "中")
    return {"add": round(add, 1), "level": level, "reasons": reasons}


def score_job(job: Job, profile: dict, today: str = "") -> tuple[int, str]:
    """加权打分 0-100 + 一句话理由。权重之和恰为 100，无需截断。

      方向匹配   30  —— canonical 方向类别命中（AI/大模型、数据开发…）
      技术相关度 18  —— tech_relevance 0-50 缩放；硬过滤已挡非技术岗，降权让位于方向
      地区       15  —— 分层：目标城市 / 一线新一线 / 其他（多城市看首城市）
      时效紧急度 10  —— 截止日越近越该先投，过期归零
      单位性质    6  —— 声望 + 勾选加成（折叠旧「心仪性质」加分组）
      招聘类型    7  —— 校招/实习等（学生优先，空=不限）
      专业匹配    6  —— 岗位专业命中画像专业（2026-09-02 新增，此前专业字段是死字段）
      学历可达  +4/-8 —— 本科可投 +4，硕博-only -8 沉底（2026-09-20 新增）
      薪资        5  —— 区间下限分档；低于画像 salary_min 时 -8
      可投递性    3  —— 链接越接近「点了就能投」越高
      标题降权  -12 —— 画像 downgrade_keywords 命中标题（如"算法"，2026-09-20 新增）
    """
    reasons: list[str] = []
    score = 0

    # 方向匹配（0-30）—— 排序主导项
    dscore, dhits = direction_match(job, profile)
    score += dscore
    if dhits:
        reasons.append("方向:" + "/".join(dhits[:2]))

    # 技术相关度（0-18）：tech_relevance 返回 0-50，等比缩放
    tech = tech_relevance(job)
    score += round(tech * 0.36)
    if tech >= 35:
        reasons.append("技术岗强相关")
    elif tech >= 15:
        reasons.append("技术岗相关")

    # 地区（0-15）分层
    cscore, creason = city_score(job, profile)
    score += cscore
    if creason:
        reasons.append(creason)

    # 时效紧急度（0-10）
    uscore, ureason = deadline_urgency(job, today)
    score += uscore
    if ureason:
        reasons.append(ureason)

    # 单位性质（0-8）：声望 + 勾选加成（合并旧「心仪性质」）
    nscore, nreason = nature_score(job, profile)
    score += nscore
    if nreason:
        reasons.append(nreason)

    # 招聘类型（0-7）：校招/实习等（学生优先，空=不限）
    rscore, rreason = recruit_type_score(job, profile)
    score += rscore
    if rreason:
        reasons.append(rreason)

    # 专业匹配（0-6）
    mscore, mreason = major_score(job, profile)
    score += mscore
    if mreason:
        reasons.append(mreason)

    # 学历可达性（+4/-8）：硕博-only 岗沉底，本科可投岗上浮
    escore, ereason = edu_score(job, profile)
    score += escore
    if ereason:
        reasons.append(ereason)

    # 薪资（0-5，低于画像门槛时 -8）
    sscore, sreason = salary_score(job, profile)
    score += sscore
    if sreason:
        reasons.append(sreason)

    # 可投递性（0-3）
    bonus = _LINK_BONUS.get(job.link_type, 0)
    score += bonus
    if bonus:
        reasons.append(job.link_type)

    # 来源降权：低相关度来源整体扣分（见 _SOURCE_PENALTY），避免稀释看板
    pen = _SOURCE_PENALTY.get((job.source or "").strip())
    if pen:
        score -= pen
        reasons.append("来源降权")

    # 标题降权词（画像 downgrade_keywords）：命中 → -12。
    # 场景：用户刻意避开算法岗，但「AI/大模型/算法」方向类别含"算法"会给纯
    # 算法岗满 30 方向分——降权让它们落到「备选」档而非霸占前排（不硬删，
    # 保留人工判断空间；JD 里提算法但标题不提的应用岗不受影响）。
    dg = [k for k in (profile.get("downgrade_keywords") or [])
          if _norm(k) in _norm(job.title or "")]
    if dg:
        score -= 12
        reasons.append("避开:" + "/".join(dg[:2]))

    return min(max(score, 0), 100), "；".join(reasons)


def recruit_type_score(job: Job, profile: dict) -> tuple[int, str]:
    """招聘类型匹配 0-7：用户画像选了校招/实习等，命中的岗位上浮。

    空选（默认）不加分也不扣分；选了校招则非校招岗在此项拿 0，自然靠后——
    学生用户想聚焦校招时很有用。
    """
    sel = profile.get("recruit_type") or []
    if not sel:
        return 0, ""
    rt = recruit_type_of(job.nature)
    if rt and rt in sel:
        return 7, f"招聘类型:{rt}"
    return 0, ""


# 分档标签：让看板能一眼分堆，而不是盯着数字猜
def score_tier(score: int) -> str:
    if score >= 75:
        return "优先投"
    if score >= 60:
        return "值得投"
    if score >= 45:
        return "备选"
    return "观察"
