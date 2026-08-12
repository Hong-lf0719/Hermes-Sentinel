"""ResumeMaker 校招雷达（resumemakeroffer.com/jobradar）采集源 —— 主源。

免费可见：公司/行业/性质/批次/工作地点/截止日/投递链接，城市字段可用，按地区过滤有效。
多页抓取（page/1..6），表格行 = 一家公司（岗位在详情页，title 用「批次·行业」近似）。
站点改版导致失败时返回空列表，不影响主流程。
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


class ResumeMakerSource(SourceAdapter):
    name = "ResumeMaker雷达"
    base = "https://www.resumemakeroffer.com/jobradar/companies/page/"

    async def fetch(self, profile) -> list[Job]:
        if not (HAS_BS4 and HAS_HTTPX):
            return []
        jobs: list[Job] = []
        try:
            async with httpx.AsyncClient(timeout=20, verify=certifi.where(), follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0"}) as cli:
                for page in range(1, 7):
                    r = await cli.get(self.base + str(page))
                    if r.status_code != 200:
                        break
                    soup = BeautifulSoup(r.text, "html.parser")
                    table = soup.find("table")
                    if not table:
                        break
                    headers = [th.get_text(strip=True) for th in table.find_all("th")]
                    idx: dict = {}
                    for i, h in enumerate(headers):
                        if "公司" in h:
                            idx["company"] = i
                        elif "行业" in h:
                            idx["industry"] = i
                        elif "性质" in h or "企业" in h:
                            idx["nature"] = i
                        elif "批次" in h or "招聘" in h:
                            idx["batch"] = i
                        elif "工作地点" in h or "地点" in h:
                            idx["city"] = i
                        elif "截止" in h:
                            idx["deadline"] = i
                        elif "操作" in h or "加入" in h:
                            idx["url"] = i
                    rows = table.find_all("tr")[1:]
                    if not rows:
                        break

                    def make(tds):
                        def g(f):
                            i = idx.get(f)
                            return tds[i].get_text(" ", strip=True) if (i is not None and i < len(tds)) else ""
                        return g

                    for tr in rows:
                        tds = tr.find_all("td")
                        if len(tds) < 3:
                            continue
                        g = make(tds)
                        company = g("company")
                        if not company:
                            continue
                        url = ""
                        i = idx.get("url")
                        if i is not None and i < len(tds):
                            a = tds[i].find("a")
                            if a and a.get("href"):
                                url = a["href"]
                        if url.startswith("/"):
                            url = "https://www.resumemakeroffer.com" + url
                        city = g("city")
                        batch = g("batch")
                        title = f"{batch}·{g('industry')}" if (batch or g("industry")) else "校招岗位"
                        jobs.append(Job(
                            company=company,
                            title=title,
                            city=city,
                            batch=batch,
                            deadline=g("deadline"),
                            url=url,
                            source=self.name,
                            nature=g("nature"),
                            industry=g("industry"),
                        ))
        except Exception:
            return []
        return jobs
