"""真实投递公司的信息补全 —— 库里没有的公司，投了之后自动搜 JD/要求/匹配度补全。

触发：浏览器扩展读「我的投递」列表页 / 聊天一句话更新，识别到库外公司时调用。
分工：web_search 的联网在 server 层（async）做；本模块只负责
「搜索结果 → 结构化补全 + 复用 matcher 打分 + 写库」，纯同步、可单测、不依赖网络。
"""
from __future__ import annotations

import re
from pathlib import Path

from .models import Job
from .matcher import score_job
from .profile import load_profile
from .store import get_db

_PROFILE_PATH = Path(__file__).resolve().parent / "profile.json"

# JD 关键句信号词：命中才视为岗位要求/职责相关
_JD_HINTS = ("职责", "要求", "任职", "技能", "经验", "熟悉", "精通", "掌握",
             "负责", "参与", "本科", "硕士", "专业", "编程", "框架", "开发")

# 行业/专业弱信号（仅用于补全后打分参考）
_INDUSTRY_SIGNALS = [
    ("人工智能", "AI", ("人工智能", "ai", "大模型", "机器学习", "深度学习", "nlp")),
    ("数据", "数据开发", ("数据", "数仓", "etl", "数据分析", "bi", "hadoop", "spark")),
    ("后端开发", "后端开发", ("后端", "java", "python", "golang", "go ", "微服务", "spring")),
    ("前端开发", "前端开发", ("前端", "vue", "react", "javascript")),
    ("测试", "测试开发", ("测试", "qa", "自动化测试")),
]


def extract_jd(search_text: str, max_chars: int = 500) -> str:
    """从搜索结果(markdown)抽取 JD 要点句子，拼接成摘要。"""
    if not search_text:
        return ""
    kept: list[str] = []
    for line in search_text.splitlines():
        line = line.strip()
        if not line or line.startswith(("🔗", "🔎", "1.", "2.", "3.", "4.", "5.")):
            continue
        if any(h in line for h in _JD_HINTS):
            for seg in re.split(r"[。；;]", line):
                s = seg.strip(" ·*-")
                if len(s) >= 4 and any(h in s for h in _JD_HINTS):
                    kept.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for k in kept:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return "；".join(out)[:max_chars]


def _guess_industry_major(text: str) -> tuple[str, str]:
    blob = (text or "").lower()
    for industry, major, kws in _INDUSTRY_SIGNALS:
        if any(k in blob for k in kws):
            return industry, major
    return "", ""


def enrich_job_from_search(raw_hash: str, company: str, title: str,
                            search_text: str, db_path: str) -> dict:
    """用搜索结果补全库外真实投递记录：JD 要点 + 匹配度 + 链接。

    返回补全字段；失败抛异常（由调用方 try 住，不阻塞主流程）。
    """
    jd = extract_jd(search_text)
    industry, major = _guess_industry_major(search_text + " " + (title or ""))
    profile = load_profile(_PROFILE_PATH)

    job = Job(
        company=company,
        title=title or "投递岗位",
        industry=industry,
        major=major,
        link_type="搜索直达" if search_text else "源详情页",
    )
    score, reason = score_job(job, profile)

    link = ""
    m = re.search(r"https?://[^\s)】]+", search_text or "")
    if m:
        link = m.group(0)

    with get_db(db_path) as c:
        c.execute(
            """UPDATE jobs
               SET match_score=?, match_reason=?, jd_text=?,
                   link_type=?, url=COALESCE(NULLIF(url, ''), ?)
               WHERE raw_hash=?""",
            (score, reason, jd, job.link_type, link, raw_hash),
        )
        c.commit()
    return {"company": company, "title": title, "match_score": score,
            "match_reason": reason, "jd_len": len(jd)}
