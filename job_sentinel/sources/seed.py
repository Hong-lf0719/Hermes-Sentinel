"""目标公司池 —— 永远成功，保证 UI 有真实可用数据。

seed_jobs.json 维护的是「武汉/孝感真实存在、常年招技术岗的雇主 + 其常设岗位方向」，
每条经 company_links 解析出实测可达的官方招聘入口。
截止日统一标「以官网为准」，不编造具体日期。

它不受反爬/会员限制影响，是线上源失效时的兜底，也是投递追踪的基础清单。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import Job
from .base import SourceAdapter


class SeedSource(SourceAdapter):
    name = "目标公司池"

    def __init__(self, json_path=None):
        self.json_path = Path(json_path) if json_path else (Path(__file__).parent.parent / "seed_jobs.json")

    async def fetch(self, profile) -> list[Job]:
        if not self.json_path.exists():
            return []
        try:
            data = json.loads(self.json_path.read_text("utf-8"))
        except Exception:
            return []
        jobs: list[Job] = []
        for d in data:
            job = Job.from_dict(d)
            job.source = job.source or self.name
            jobs.append(job)
        return jobs
