"""offer多多（givemeoc.com）采集源 —— best-effort。

注意：该站免费层城市/投递链接为「登录后可见」，故 city/url 多数缺失。
作为大厂广撒网补充源，城市未知时仍收录（硬过滤对未知城市放行），但降权。
若站点改版导致解析失败，fetch 返回空列表并留日志，不影响主流程。
"""
from __future__ import annotations

import certifi
from ..models import Job
from .base import SourceAdapter

try:
    import httpx
    from bs4 import BeautifulSoup
    HAS_HTTPX = True
    HAS_BS4 = True
except ImportError:
    HAS_HTTPX = False
    HAS_BS4 = False

COL_MAP = {
    "公司名称": "company", "公司": "company",
    "公司类型": "nature", "所属行业": "industry",
    "招聘类型": "batch", "招聘对象": "grade",
    "工作地点": "city", "岗位": "title",
    "投递截止": "deadline", "相关链接": "url", "招聘公告": "url",
}


class GivemeocSource(SourceAdapter):
    name = "offer多多"
    base = "https://www.givemeoc.com"

    async def fetch(self, profile) -> list[Job]:
        if not (HAS_BS4 and HAS_HTTPX):
            return []
        jobs: list[Job] = []
        try:
            async with httpx.AsyncClient(timeout=20, verify=certifi.where(), follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0"}) as cli:
                r = await cli.get(self.base)
                if r.status_code != 200:
                    return []
                html = r.text
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table")
            if not table:
                return []
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            idx: dict = {}
            for i, h in enumerate(headers):
                for key, field in COL_MAP.items():
                    if key in h:
                        idx[field] = i
            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) < len(headers):
                    continue

                def get(field):
                    i = idx.get(field)
                    if i is None or i >= len(tds):
                        return ""
                    return tds[i].get_text(" ", strip=True)

                company = get("company")
                if not company:
                    continue
                url = ""
                i = idx.get("url")
                if i is not None and i < len(tds):
                    a = tds[i].find("a")
                    if a and a.get("href"):
                        url = a["href"]
                city = get("city")
                if city in ("登录后可见", "会员可见", "未知", ""):
                    city = ""
                title = get("title") or (f"{get('batch')}·{get('industry')}" if get("batch") or get("industry") else "校招岗位")
                jobs.append(Job(
                    company=company,
                    title=title,
                    city=city,
                    education="",
                    batch=get("batch"),
                    deadline=get("deadline"),
                    url=url,
                    source=self.name,
                    nature=get("nature"),
                    industry=get("industry"),
                ))
        except Exception:
            return []
        return jobs
