"""job_sentinel —— Hermes Sentinel 秋招哨兵模块。

职责：按用户画像（地区/方向/学历/院校/批次）扫描多渠道校招岗位，
硬过滤 + 规则匹配打分，存入 SQLite 岗位库，供前端卡片流与投递追踪使用。

模块化设计：不依赖 server.py，可单独 import 测试：
    from job_sentinel import scan_and_match
    res = await scan_and_match(db_path, profile_path, seed_path)
"""
from __future__ import annotations

from .scan import scan_and_match
from .profile import load_profile, save_profile, DEFAULT_PROFILE
from .store import get_db, get_jobs, update_status, job_stats, upsert_job
from .models import Job, VALID_STATUS, VALID_BATCH

__all__ = [
    "scan_and_match",
    "load_profile", "save_profile", "DEFAULT_PROFILE",
    "get_db", "get_jobs", "update_status", "job_stats", "upsert_job",
    "Job", "VALID_STATUS", "VALID_BATCH",
]
