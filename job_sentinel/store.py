"""SQLite 岗位存储 —— jobs 表 + 投递状态追踪。

独立管理一份 jobs.db（默认与 Hermes 同目录），也可由 server.py 传入 hermes.db 同库。
模块化设计：job_sentinel 不依赖 server.py，可单独测试。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import Job, VALID_STATUS
from .matcher import (direction_match, recruit_type_score, canon_major, _norm,
                      split_cities)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT, title TEXT, city TEXT, education TEXT, batch TEXT,
    deadline TEXT, url TEXT, source TEXT, nature TEXT, industry TEXT,
    major TEXT, school_level TEXT, salary TEXT, first_seen TEXT,
    match_score INTEGER DEFAULT 0, match_reason TEXT, status TEXT DEFAULT '新',
    raw_hash TEXT UNIQUE, link_type TEXT DEFAULT '', user_hidden INTEGER DEFAULT 0
);
"""

# 后加的列，需对已有库做迁移（SQLite 不支持 IF NOT EXISTS 加列）
MIGRATIONS = [
    ("link_type", "TEXT DEFAULT ''"),
    ("jd_text", "TEXT DEFAULT ''"),
    ("user_hidden", "INTEGER DEFAULT 0"),
    ("enterprise_type", "TEXT DEFAULT ''"),
    ("exam_free", "TEXT DEFAULT ''"),
]


def _migrate(c):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    for name, decl in MIGRATIONS:
        if name not in cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    c.commit()


def _conn(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    c.execute(SCHEMA)
    c.commit()
    _migrate(c)
    return c


@contextmanager
def get_db(db_path):
    c = _conn(db_path)
    try:
        yield c
    finally:
        c.close()


def upsert_job(c, job: Job):
    job.raw_hash = job.dedup_hash()
    if not job.first_seen:
        job.first_seen = datetime.now().strftime("%Y-%m-%d")
    c.execute(
        """INSERT INTO jobs (
            company,title,city,education,batch,deadline,url,source,nature,industry,
            major,school_level,salary,first_seen,match_score,match_reason,status,raw_hash,link_type,
            jd_text,enterprise_type,exam_free
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(raw_hash) DO UPDATE SET
            match_score=excluded.match_score, match_reason=excluded.match_reason,
            deadline=excluded.deadline, url=excluded.url, batch=excluded.batch,
            city=excluded.city, education=excluded.education, school_level=excluded.school_level,
            link_type=excluded.link_type, jd_text=excluded.jd_text,
            enterprise_type=excluded.enterprise_type, exam_free=excluded.exam_free
        """,
        (job.company, job.title, job.city, job.education, job.batch, job.deadline,
         job.url, job.source, job.nature, job.industry, job.major, job.school_level,
         job.salary, job.first_seen, job.match_score, job.match_reason, job.status,
         job.raw_hash, job.link_type, job.jd_text, job.enterprise_type, job.exam_free),
    )
    c.commit()


def get_jobs(c, filters: dict | None = None) -> list[Job]:
    filters = filters or {}
    sql = "SELECT * FROM jobs WHERE 1=1"
    args: list = []
    if filters.get("status"):
        sql += " AND status=?"; args.append(filters["status"])
    if filters.get("batch"):
        sql += " AND batch=?"; args.append(filters["batch"])
    if filters.get("city"):
        sql += " AND city LIKE ?"; args.append(f"%{filters['city']}%")
    if filters.get("nature"):
        sql += " AND nature LIKE ?"; args.append(f"%{filters['nature']}%")
    if filters.get("enterprise_type"):
        sql += " AND enterprise_type=?"; args.append(filters["enterprise_type"])
    if filters.get("exam_free"):
        sql += " AND exam_free=?"; args.append(filters["exam_free"])
    if filters.get("min_score"):
        sql += " AND match_score>=?"; args.append(filters["min_score"])
    if filters.get("max_score"):
        sql += " AND match_score<=?"; args.append(filters["max_score"])
    if filters.get("source"):
        sql += " AND source LIKE ?"; args.append(f"%{filters['source']}%")
    # 用户「标记不投」的岗位默认隐藏（看板过滤，非删除）；include_hidden 才显示
    if not filters.get("include_hidden"):
        sql += " AND (user_hidden IS NULL OR user_hidden=0)"

    # 排序可切换：默认按匹配分，也支持「截止日最近优先」「最新发现优先」
    # deadline 是混合格式文本（92% 是 ISO '2026-09-01'，其余为 '以官网为准' 等），
    # 分三组排：① 未过期的真实日期（越近越前）② 无法判断的 ③ 已过期沉底
    # —— 否则 2025 年的过期岗会因字典序最小而霸占首屏
    sort = filters.get("sort") or "score"
    if sort == "deadline":
        sql += (" ORDER BY CASE"
                " WHEN deadline GLOB '20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
                "      AND deadline < date('now') THEN 2"
                " WHEN deadline GLOB '20[0-9][0-9]*' THEN 0"
                " ELSE 1 END,"
                " deadline ASC, match_score DESC")
    elif sort == "new":
        sql += " ORDER BY first_seen DESC, match_score DESC"
    else:
        sql += " ORDER BY match_score DESC, first_seen DESC"
    rows = c.execute(sql, args).fetchall()
    jobs = [Job.from_dict(dict(r)) for r in rows]

    # —— 看板层语义过滤（需模糊文本匹配 / 画像上下文，SQL 不便做）——
    # 地区聚焦：仅保留命中画像地区的岗位（「只看目标地区」开关触发）。
    # 城市未标注的岗保留（仅十余条，宁可多看一眼也不静默丢岗）。
    regions = filters.get("regions")
    if regions:
        def _region_ok(j: Job) -> bool:
            city = (j.city or "").strip()
            if not city:
                return True          # 未知地区：保留
            parts = split_cities(city)
            if not parts:
                return True
            return any(any((r in p or p in r) for r in regions) for p in parts)
        jobs = [j for j in jobs if _region_ok(j)]

    # 学历门槛：按 education 文本推断可达性档位过滤。
    # 阈值取自画像 edu_threshold（本科可投 / 需硕士 / 不限），
    # 2026-09-02 修正：此前后端硬编码「本科可投」，画像里改门槛毫无效果。
    edu_thr = filters.get("edu_threshold")
    if edu_thr == "本科可投":
        jobs = [j for j in jobs if _edu_bucket(j.education) != "硕士及以上"]
    elif edu_thr == "需硕士":
        jobs = [j for j in jobs if _edu_bucket(j.education) == "硕士及以上"]
    # 薪资门槛：能解析出月薪 K 且低于画像 salary_min 的岗剔除（面议/空不剔除）
    smin = filters.get("salary_min") or 0
    if smin > 0:
        from .matcher import salary_k
        kept = []
        for j in jobs:
            k, _ = salary_k(j.salary or "")
            if k is not None and k < smin:
                continue
            kept.append(j)
        jobs = kept
    # 排除词：看板层硬过滤。此前 exclude_keywords 只在扫描入库的 hard_filter 生效，
    # 用户改排除词后已入库的岗位一个不动 —— 「改了画像却没变化」的硬伤。
    excl = [e for e in (filters.get("exclude_keywords") or []) if e]
    if excl:
        def _not_excluded(j: Job) -> bool:
            blob = _norm(f"{j.company or ''} {j.title or ''} {j.industry or ''} {j.major or ''}")
            return not any(_norm(e) in blob for e in excl)
        jobs = [j for j in jobs if _not_excluded(j)]
    # 画像聚焦：仅保留命中用户「求职方向 / 专业 / 招聘类型」的岗（软过滤，
    # 信号缺失的岗保留，避免误伤未抓到 JD 的岗）。改画像后开启即让岗位集随画像变化。
    if filters.get("profile_focus"):
        subset = filters.get("_profile") or {}
        jobs = [j for j in jobs if _job_profile_relevant(j, subset)]
    return jobs


def rescore_all(c, profile: dict) -> dict:
    """用当前画像与打分规则重算全库分数。

    用途：改了 profile（换城市偏好 / 加方向）或升级打分规则后，
    不必重新抓取（耗时数分钟且依赖外部站可用），秒级重排即可。
    投递状态、jd_text 等用户数据一律不动。
    """
    from .matcher import score_job

    rows = c.execute("SELECT * FROM jobs").fetchall()
    changed = 0
    for r in rows:
        job = Job.from_dict(dict(r))
        new_score, new_reason = score_job(job, profile)
        if new_score != (r["match_score"] or 0) or new_reason != (r["match_reason"] or ""):
            c.execute(
                "UPDATE jobs SET match_score=?, match_reason=? WHERE raw_hash=?",
                (new_score, new_reason, r["raw_hash"]),
            )
            changed += 1
    c.commit()
    return {"total": len(rows), "changed": changed}


def _edu_bucket(e: str) -> str:
    """把岗位学历要求归为可达性档位。"""
    e = (e or "").replace(" ", "")
    if "硕士" in e or "研究生" in e or "博士" in e:
        return "本科" if "本科" in e else "硕士及以上"
    if "大专" in e or "专科" in e:
        return "大专"
    if "本科" in e:
        return "本科"
    if "不限" in e or "无学历" in e:
        return "本科"
    return "其他"


def _job_profile_relevant(job: "Job", subset: dict) -> bool:
    """画像聚焦软过滤：岗位是否命中用户任一圈选维度（方向 / 专业 / 招聘类型）。

    信号缺失（标题 / 行业 / 专业皆空）的岗一律保留——避免误伤未抓到 JD 的岗。
    其余岗需命中至少一个维度才保留，使「改画像」能真正改变可见岗位集，
    而不只是重排匹配分。
    """
    title = (job.title or "").strip()
    industry = (job.industry or "").strip()
    major = (job.major or "").strip()
    if not (title or industry or major):
        return True  # 无信号不惩罚

    # 求职方向：复用 matcher.direction_match，命中即相关
    dirs = subset.get("directions") or []
    if dirs and direction_match(job, {"directions": dirs})[1]:
        return True

    # 专业：canonical 精确命中 或 文本包含
    majors = subset.get("major") or []
    if majors:
        mset = {canon_major(m) for m in majors}
        if canon_major(major) in mset:
            return True
        blob = _norm(f"{title} {industry} {major}")
        if any(_norm(m) in blob for m in majors):
            return True

    # 招聘类型：校招 / 实习等（学生优先），命中即相关
    rts = subset.get("recruit_type") or []
    if rts and recruit_type_score(job, {"recruit_type": rts})[1]:
        return True

    return False


def get_profile_options(c) -> dict:
    """把岗位库真实分布聚合成画像可选项，让用户「勾选」而非「瞎填」。

    关键修复：之前城市/性质/行业满是重复（"武汉"vs"武汉,广东省深圳"、
    "国企"vs"国有企业"、社招/校招混在性质里）。这里全部归一：
      - 城市：拆逗号 + 去「省/市/自治区」后缀 + 累加计数
      - 性质：从 nature 拆出「招聘类型」与「单位性质」两类 canonical
      - 方向：基于 DIRECTION_CATEGORIES 统计每个类别实际命中的岗位数
      - 专业：canon_major 归并
      - 学历：_edu_bucket 归并；并附 edu_breakdown 供「可达机会」展示
    """
    from .matcher import (DIRECTION_CATEGORIES, canon_natures, recruit_type_of,
                          canon_major, _norm)
    from collections import Counter

    # 城市：拆逗号 + 去行政后缀 + 归一，累加计数
    city_counter: Counter = Counter()
    for (city,) in c.execute("SELECT city FROM jobs WHERE city IS NOT NULL AND city<>''"):
        for part in city.split(","):
            p = part.strip()
            p = p.replace("特别行政区", "").replace("自治区", "").replace("自治州", "")
            p = p.strip("省市").strip()
            if p:
                city_counter[p] += 1
    cities = [[k, n] for k, n in city_counter.most_common(25)]

    # 招聘类型 + 单位性质（都从 nature 拆出，解决社招/校招混入性质的混乱）
    rt_counter: Counter = Counter()
    nat_counter: Counter = Counter()
    for (nat,) in c.execute("SELECT nature FROM jobs WHERE nature IS NOT NULL AND nature<>''"):
        rt = recruit_type_of(nat)
        if rt:
            rt_counter[rt] += 1
        for cn in canon_natures(nat):
            nat_counter[cn] += 1
    recruit_types = [[k, rt_counter[k]] for k in ("校招", "社招", "实习", "见习") if rt_counter.get(k)]
    natures = [[k, nat_counter[k]] for k in
               ("央企", "国企", "事业单位", "上市公司", "外企", "民企") if nat_counter.get(k)]

    # 方向类别：统计每个类别实际命中的岗位数
    blobs = [(_norm(f"{r['title']} {r['industry']} {r['major']}"))
             for r in c.execute("SELECT title,industry,major FROM jobs")]
    directions = []
    for label, kws in DIRECTION_CATEGORIES.items():
        n = sum(1 for b in blobs if any(w in b for w in kws))
        if n:
            directions.append([label, n])
    directions.sort(key=lambda x: -x[1])

    # 专业：归一后计数
    major_counter: Counter = Counter()
    for (m,) in c.execute("SELECT major FROM jobs WHERE major IS NOT NULL AND major<>''"):
        major_counter[canon_major(m)] += 1
    majors = [[k, major_counter[k]] for k in
              ("计算机类", "软件工程", "电子信息", "通信", "自动化", "数据科学/人工智能", "其他")
              if major_counter.get(k)]

    # 学历：归并 + 可达性分布
    edu_counter: Counter = Counter()
    for (e,) in c.execute("SELECT education FROM jobs WHERE education IS NOT NULL AND education<>''"):
        edu_counter[_edu_bucket(e)] += 1
    educations = [[k, edu_counter[k]] for k in ("本科", "硕士及以上", "大专", "其他") if edu_counter.get(k)]

    # 学历门槛（看板过滤用）：不限 / 本科可投 / 需硕士
    total_edu = sum(edu_counter.values()) or 1
    edu_thresholds = [
        {"val": "不限", "desc": f"全部 {total_edu} 个岗位"},
        {"val": "本科可投", "desc": f"本科可投 {edu_counter.get('本科',0)+edu_counter.get('大专',0)+edu_counter.get('其他',0)} / 共 {total_edu}"},
        {"val": "需硕士", "desc": f"仅硕士及以上 {edu_counter.get('硕士及以上',0)}"},
    ]

    return {
        "cities": cities,
        "recruit_types": recruit_types,
        "natures": natures,
        "directions": directions,
        "majors": majors,
        "educations": educations,
        "edu_thresholds": edu_thresholds,
        "edu_breakdown": dict(edu_counter),
    }


def update_status(c, raw_hash: str, status: str) -> bool:
    """回写投递状态。非法状态拒绝写入（不静默重置为「新」，否则畸形请求能把 OC 打回「新」）。"""
    if status not in VALID_STATUS:
        return False
    c.execute("UPDATE jobs SET status=? WHERE raw_hash=?", (status, raw_hash))
    c.commit()
    return True


def set_hidden(c, raw_hash: str, hidden: bool):
    """用户「标记不投」/ 恢复。隐藏是看板过滤（非删除），rescore/扫描都不动它。"""
    c.execute("UPDATE jobs SET user_hidden=? WHERE raw_hash=?",
              (1 if hidden else 0, raw_hash))
    c.commit()


def adopt_fill(c, raw_hash: str, fill: dict):
    """采纳「补全建议」：把推断的 industry/major 写回 job 记录（仅补空字段）。"""
    cur = c.execute("SELECT industry,major FROM jobs WHERE raw_hash=?", (raw_hash,)).fetchone()
    if not cur:
        return False
    ind, maj = cur["industry"], cur["major"]
    updates = {}
    if not (ind or "").strip() and fill.get("industry"):
        updates["industry"] = fill["industry"]
    if not (maj or "").strip() and fill.get("major"):
        updates["major"] = fill["major"]
    if not updates:
        return False
    set_clause = ", ".join(f"{k}=?" for k in updates)
    c.execute(f"UPDATE jobs SET {set_clause} WHERE raw_hash=?", list(updates.values()) + [raw_hash])
    c.commit()
    return True


def prune_invalid(c, keep_fn) -> int:
    """清理不再符合当前匹配规则的历史岗位。

    用户已产生投递行为的（已投/笔试/面试/挂/OC）一律保留 —— 那是他的投递记录，
    规则收紧不该抹掉历史。只清理还停留在「新 / 已看」的噪声条目。
    """
    protected = ("已投", "笔试", "面试", "挂", "OC")
    removed = 0
    # user_hidden=0：「标记不投」是看板过滤（非删除），清理不得触碰
    rows = c.execute(
        "SELECT * FROM jobs WHERE status NOT IN (?,?,?,?,?) AND user_hidden=0", protected
    ).fetchall()
    for r in rows:
        job = Job.from_dict(dict(r))
        if not keep_fn(job):
            c.execute("DELETE FROM jobs WHERE raw_hash=?", (job.raw_hash,))
            removed += 1
    c.commit()
    return removed


def job_stats(c) -> dict:
    total = c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    by_status: dict = {}
    for r in c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status"):
        by_status[r["status"]] = r["n"]
    return {"total": total, "by_status": by_status, "new": by_status.get("新", 0)}
