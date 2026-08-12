"""国聘网（iguopin.com）采集源 —— 全国性央国企校招岗位。

为什么选它：
  * 国务院国资委指导、国投人力运营，5 万+企业入驻，500 万+岗位
  * 覆盖全国所有省份，补齐武大就业网仅限湖北的短板
  * 列表接口一次返回完整数据（含 JD 正文 + 城市 + 薪资），无需额外拉详情

接口（需登录态 Applauth token）：
  POST https://gp-api.iguopin.com/api/jobs/v1/list
  Header: Applauth=<token>
  Body: {page, page_size, keyword}
  Response: {total, page, page_size, list[{job_id, job_name, company_name, ...,
           district_list[{area_code, area_cn}], contents, ...}]}

认证：
  * token 从环境变量 GUOPIN_TOKEN 读取（浏览器 F12→Network→请求头 Applauth）
  * 未配置时整个源静默降级（返回空列表，不影响其他源）

抓取策略：
  1. 按技术关键词分批搜索（数据/AI/算法/开发…）
  2. 全量拉完（最多 GP_MAX_PAGES 页），客户端按地区码过滤
  3. 限速：每页间隔 GP_DELAY 秒，避免触发风控
"""
from __future__ import annotations

import asyncio
import os
import re

import certifi
from ..models import Job
from .base import SourceAdapter

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

BASE = "https://gp-api.iguopin.com"
LIST_URL = f"{BASE}/api/jobs/v1/list"

# 国聘详情页（每条岗位都有独立页）
DETAIL_TPL = "https://www.iguopin.com/job/detail?id={job_id}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 搜索关键词矩阵——覆盖数据科学 / AI 应用开发 方向
KEYWORDS = [
    "数据", "人工智能", "算法", "软件开发", "大数据",
    "Python", "Java", "前端", "后端", "数据分析",
    "机器学习", "深度学习", "数据开发", "数仓",
]

GP_MAX_PAGES = 8          # 单个关键词最多翻几页（8*20=160 条/词）
GP_PAGE_SIZE = 20         # 每页条数
GP_DELAY = 0.8            # 页间间隔（秒）


def _token() -> str | None:
    """从环境变量读取国聘 Applauth token。"""
    return os.getenv("GUOPIN_TOKEN") or os.getenv("GUOPIN_COOKIE") or None


def _clean_city(district_list: list[dict] | None) -> str:
    """从 district_list 提取城市名。"""
    if not district_list:
        return ""
    # 取第一个工作地点的城市部分："银川-西夏区" -> "银川"
    area = (district_list[0].get("area_cn") or "").strip()
    if "-" in area:
        return area.split("-")[0]
    return area or ""


def _build_salary(item: dict) -> str:
    """拼接薪资范围。"""
    mn = item.get("min_wage") or 0
    mx = item.get("max_wage") or 0
    unit = item.get("wage_unit_cn") or ""
    if mn == 0 and mx == 0:
        return "面议"
    parts = []
    if mn > 0:
        parts.append(str(int(mn)))
    if mx > 0 and mx != mn:
        parts.append(str(int(mx)))
    return f"{chr(126).join(parts)} {unit}".strip() if parts else "面议"


def _map_nature(nature_cn: str) -> str:
    """将国聘的 nature_cn 归一化为 央企/国企/民企/外企。"""
    s = (nature_cn or "").strip()
    if any(k in s for k in ["央企", "中央"]):
        return "央企"
    if any(k in s for k in ["国企", "国有"]):
        return "国企"
    if any(k in s for k in ["外企", "合资", "独资"]):
        return "外企"
    if any(k in s for k in ["民营", "私企", "民企"]):
        return "民企"
    return s or ""


def _map_batch(recruitment_type_cn: str, is_graduates: bool) -> str:
    """判断是否校招批次。"""
    t = (recruitment_type_cn or "")
    if is_graduates or "校园" in t or "校招" in t:
        return "秋招"
    if "实习" in t:
        return "实习"
    return ""


class GuopinSource(SourceAdapter):
    name = "国聘网"

    async def _page(self, cli: httpx.AsyncClient, token: str,
                    page: int = 1, keyword: str = "") -> tuple[list[dict], int]:
        """取一页岗位列表。"""
        body: dict[str, int | str] = {"page": page, "page_size": GP_PAGE_SIZE}
        if keyword:
            body["keyword"] = keyword
        try:
            r = await cli.post(
                LIST_URL,
                headers={
                    "User-Agent": UA,
                    "Referer": "https://xiaoyuan.iguopin.com/",
                    "Origin": "https://xiaoyuan.iguopin.com",
                    "Applauth": token,
                },
                json=body,
                timeout=25,
            )
            r.raise_for_status()
            data = (r.json() or {}).get("data") or {}
            return data.get("list") or [], int(data.get("total") or 0)
        except Exception:
            return [], 0

    async def _collect_keyword(self, cli: httpx.AsyncClient,
                               token: str, kw: str) -> list[dict]:
        """按关键词翻完所有页。"""
        items: list[dict] = []
        first, total = await self._page(cli, token, 1, keyword=kw)
        items.extend(first)
        max_pages = min(
            (total + GP_PAGE_SIZE - 1) // GP_PAGE_SIZE,
            GP_MAX_PAGES,
        ) if total else 1
        if max_pages > 1:
            for p in range(2, max_pages + 1):
                await asyncio.sleep(GP_DELAY)
                lst, _ = await self._page(cli, token, p, keyword=kw)
                items.extend(lst)
        return items

    @staticmethod
    def _to_job(item: dict) -> Job | None:
        """把一条 API 记录转成 Job。"""
        title = (item.get("job_name") or "").strip()
        company = (item.get("company_name") or "").strip()
        if not title or not company:
            return None
        jid = item.get("job_id") or ""
        return Job(
            company=company,
            title=title,
            city=_clean_city(item.get("district_list")),
            education=item.get("education_cn") or "",
            batch=_map_batch(
                item.get("recruitment_type_cn", ""),
                bool(item.get("is_graduates")),
            ),
            deadline=(item.get("end_time") or "")[:10],
            url=DETAIL_TPL.format(job_id=jid) if jid else "",
            link_type="简章详情",
            source="国聘网",
            nature=_map_nature(item.get("nature_cn") or ""),
            industry=item.get("category_cn") or "",
            major="、".join(item.get("major_cn") or []),
            salary=_build_salary(item),
        )

    async def fetch(self, profile: dict) -> list[Job]:  # noqa: D102
        if not HAS_HTTPX:
            return []

        token = _token()
        if not token:
            return []

        limits = httpx.Limits(max_connections=4)
        async with httpx.AsyncClient(
            timeout=25, verify=certifi.where(), follow_redirects=True, limits=limits,
            headers={"User-Agent": UA},
        ) as cli:
            all_items: list[dict] = []
            seen_ids: set[str] = set()

            # 关键词搜索 + 去重
            for kw in KEYWORDS:
                items = await self._collect_keyword(cli, token, kw)
                for it in items:
                    jid = it.get("job_id")
                    if jid and jid not in seen_ids:
                        seen_ids.add(jid)
                        all_items.append(it)

            jobs: list[Job] = []
            for it in all_items:
                j = self._to_job(it)
                if j:
                    jobs.append(j)
            return jobs
