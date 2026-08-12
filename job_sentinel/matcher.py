"""匹配引擎 —— 三级过滤 + 加权打分 + 批次识别。

设计取舍：用规则打分而非逐岗调 LLM，省 token、快、可复现。
过滤顺序（越早淘汰成本越低）：
  1. 行业黑名单  —— 养殖/种业/快消这类与技术岗无关的整条丢弃
  2. 技术相关度  —— 岗位标题/专业不含任何技术信号则丢弃（核心修复）
  3. 硬条件      —— 批次 / 学历 / 城市 / 用户自定义排除词
"""
from __future__ import annotations

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


def recognize_batch(text: str) -> str:
    """从文本识别招聘批次。"""
    t = text or ""
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

    title_hits = [k for k in TECH_KEYWORDS if k in title]
    other_hits = [k for k in TECH_KEYWORDS if k in blob and k not in title_hits]

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


_EDU_RANK = {"专科": 1, "大专": 1, "本科": 2, "学士": 2, "硕士": 3, "研究生": 3, "博士": 4}


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


def hard_filter(job: Job, profile: dict) -> bool:
    """三级过滤。任一不过直接丢弃，不进入打分与入库。"""
    blob = _norm(f"{job.company} {job.title} {job.industry} {job.major}")

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

    # 3b. 学历
    if not _edu_ok(job.education, profile.get("education", "本科")):
        return False

    # 3c. 城市（空城市视为未知，广收录，由打分降权）
    regions = profile.get("regions", [])
    city = (job.city or "").strip()
    if city and regions:
        if not any((r in city or city in r) for r in regions):
            return False

    # 3d. 用户自定义排除词
    excl = profile.get("exclude_keywords", [])
    if excl and any(k in blob for k in excl):
        return False

    return True


def score_job(job: Job, profile: dict) -> tuple[int, str]:
    """加权打分 0-100 + 一句话理由。

    权重分配（技术相关度占大头，杜绝"养猪岗拿 30 分"）：
      技术相关度 50 / 方向命中 25 / 地区 12 / 央国企 8 / 本科可投 3 / 有直达链接 2
    """
    reasons: list[str] = []

    # 技术相关度（0-50）
    tech = tech_relevance(job)
    score = tech
    if tech >= 35:
        reasons.append("技术岗强相关")
    elif tech >= 15:
        reasons.append("技术岗相关")
    else:
        reasons.append("技术弱相关")

    # 方向命中（0-25）
    dirs = profile.get("directions", [])
    blob = _norm(f"{job.title} {job.industry} {job.major}")
    hit = [d for d in dirs if _norm(d) in blob]
    if hit:
        score += 25
        reasons.append("方向:" + "/".join(hit[:2]))

    # 地区（0-12）
    regions = profile.get("regions", [])
    if job.city and any((r in job.city or job.city in r) for r in regions):
        score += 12
        reasons.append("地区匹配")

    # 单位性质（0-8）
    if "央企" in (job.nature or "") or "国企" in (job.nature or ""):
        score += 8
        reasons.append("央国企稳")

    # 学历（0-3）
    if "本科" in (job.education or "") or not job.education:
        score += 3

    # 可投递性（0-5）：链接越接近「点了就能投」，加分越高
    _LINK_BONUS = {"官方网申": 5, "简章详情": 4, "官方校招页": 2, "源详情页": 1}
    bonus = _LINK_BONUS.get(job.link_type, 0)
    if bonus:
        score += bonus
        reasons.append(job.link_type)

    return min(score, 100), "；".join(reasons)
