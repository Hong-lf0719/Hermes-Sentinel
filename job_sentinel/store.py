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

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT, title TEXT, city TEXT, education TEXT, batch TEXT,
    deadline TEXT, url TEXT, source TEXT, nature TEXT, industry TEXT,
    major TEXT, school_level TEXT, salary TEXT, first_seen TEXT,
    match_score INTEGER DEFAULT 0, match_reason TEXT, status TEXT DEFAULT '新',
    raw_hash TEXT UNIQUE, link_type TEXT DEFAULT ''
);
"""

# 后加的列，需对已有库做迁移（SQLite 不支持 IF NOT EXISTS 加列）
MIGRATIONS = [("link_type", "TEXT DEFAULT ''"), ("jd_text", "TEXT DEFAULT ''")]


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
            major,school_level,salary,first_seen,match_score,match_reason,status,raw_hash,link_type
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(raw_hash) DO UPDATE SET
            match_score=excluded.match_score, match_reason=excluded.match_reason,
            deadline=excluded.deadline, url=excluded.url, batch=excluded.batch,
            city=excluded.city, education=excluded.education, school_level=excluded.school_level,
            link_type=excluded.link_type
        """,
        (job.company, job.title, job.city, job.education, job.batch, job.deadline,
         job.url, job.source, job.nature, job.industry, job.major, job.school_level,
         job.salary, job.first_seen, job.match_score, job.match_reason, job.status,
         job.raw_hash, job.link_type),
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
    if filters.get("min_score"):
        sql += " AND match_score>=?"; args.append(filters["min_score"])
    sql += " ORDER BY match_score DESC, first_seen DESC"
    rows = c.execute(sql, args).fetchall()
    return [Job.from_dict(dict(r)) for r in rows]


def update_status(c, raw_hash: str, status: str):
    if status not in VALID_STATUS:
        status = "新"
    c.execute("UPDATE jobs SET status=? WHERE raw_hash=?", (status, raw_hash))
    c.commit()


def prune_invalid(c, keep_fn) -> int:
    """清理不再符合当前匹配规则的历史岗位。

    用户已产生投递行为的（已投/笔试/面试/挂/OC）一律保留 —— 那是他的投递记录，
    规则收紧不该抹掉历史。只清理还停留在「新 / 已看」的噪声条目。
    """
    protected = ("已投", "笔试", "面试", "挂", "OC")
    removed = 0
    rows = c.execute("SELECT * FROM jobs WHERE status NOT IN (?,?,?,?,?)", protected).fetchall()
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
