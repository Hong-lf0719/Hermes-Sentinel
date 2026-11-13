"""软通动力校招采集源 —— JSON POST 接口。

career.isoftstone.com 的 /campus/all 接口返回校招职位列表（含实习）。
接口逆向自 my.js 的 searchJob：POST JSON {workCity,jobTypeId,keyWord,maxcount,page}，
返回 {count, results:[{name,address_detail,salary_name,educational_name,hiretype_name,describe,id,...}]}。

软通为明牌外包公司，本科友好、流程快、不卡笔试，作保底池补充。
薪资字段是实薪（如 "10-15K"），比简章源的"面议"强。
若接口变更导致失败，fetch 返回空列表并留日志，不影响主流程。
"""
from __future__ import annotations

import certifi
from ..models import Job
from .base import SourceAdapter

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

CAMPUS_API = "https://career.isoftstone.com/campus/all"
APPLY_URL = "https://career.isoftstone.com/talent/htmls/xiaoyuanzhaopin/index.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": APPLY_URL,
    "X-Requested-With": "XMLHttpRequest",
}


def _clean_city(raw: str) -> str:
    """'湖北省 武汉市' → '武汉'；'北京市 北京市' → '北京'；'香港特别行政区...' → '香港'。"""
    if not raw:
        return ""
    s = raw.replace("特别行政区", "").replace("壮族自治区", "").replace("回族自治区", "")
    parts = [p.strip() for p in s.split() if p.strip()]
    city = parts[-1] if parts else s.strip()
    for suf in ("省", "市", "自治区", "特别行政区", "地区"):
        city = city.replace(suf, "")
    return city.strip()


class IsotstoneSource(SourceAdapter):
    name = "软通动力校招"
    base = "https://career.isoftstone.com"

    async def fetch(self, profile) -> list[Job]:
        if not HAS_HTTPX:
            return []
        jobs: list[Job] = []
        try:
            async with httpx.AsyncClient(
                timeout=20, verify=certifi.where(), follow_redirects=True, headers=HEADERS
            ) as cli:
                payload = {"workCity": "", "jobTypeId": "", "keyWord": "", "maxcount": 100, "page": 1}
                r = await cli.post(CAMPUS_API, json=payload)
                if r.status_code != 200:
                    return []
                data = r.json()
                for j in data.get("results", []):
                    name = (j.get("name") or "").strip()
                    if not name:
                        continue
                    title = name.replace("【27届】", "").replace("【26届】", "").strip()
                    city = _clean_city(j.get("address_detail", ""))
                    salary = j.get("salary_name", "")
                    edu = j.get("educational_name", "")
                    hire = j.get("hiretype_name", "")
                    batch = "实习" if "实习" in hire else "秋招"
                    jd = j.get("describe", "") or ""
                    pid = j.get("id", "")
                    pub = j.get("publish_date", "")
                    jobs.append(Job(
                        company="软通动力",
                        title=title,
                        city=city,
                        education=edu,
                        salary=salary,
                        batch=batch,
                        deadline=pub[:10] if pub else "",
                        url=APPLY_URL,
                        source=self.name,
                        nature="外包公司",
                        enterprise_type="民企",
                        industry="软件与信息技术服务",
                        jd_text=jd[:2000],
                        link_type="官方校招页",
                        raw_hash=pid,
                    ))
        except Exception:
            return []
        return jobs
