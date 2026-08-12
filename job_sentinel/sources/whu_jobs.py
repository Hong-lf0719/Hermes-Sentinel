"""武汉大学就业信息网采集源 —— 岗位级富矿。

为什么它是主源：
  * 全站 4900+ 条校招简章，湖北本地企业密度远高于全国性聚合站
  * 详情接口返回 **具体岗位列表**（岗位名/城市/学历/专业/薪资/JD 全文），
    而不是聚合站那种「一条 = 一家公司 + 行业标签」的粗粒度条目
  * 简章正文里常带企业自己的网申表单地址 —— 这是真正「点了就能投」的链接

接口（由页面 JS 逆向得到，无需登录）：
  GET  /f/ajaxHome/getToken                     -> {"data": "<token>"}
  POST /f/recruitmentinfo/ajax_frontRecruitinfo -> 简章列表（分页/关键词/岗位类型）
  POST /f/recruitmentinfo/ajax_show             -> 简章详情（含 recruitmentPositionList）
  两个 POST 都需 header: token

抓取策略（控制请求量，避免给对方站点压力）：
  1. 关键词矩阵搜索：地域词（武汉/湖北/孝感）+ 技术词（数据/算法/人工智能…）
  2. 列表结果去重，地域命中的优先，总量截断到 DETAIL_LIMIT
  3. 并发拉详情，把 recruitmentPositionList 展开成一条条具体岗位
"""
from __future__ import annotations

import asyncio
import re

import certifi
from ..models import Job
from .base import SourceAdapter

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - 环境缺依赖时整个源降级
    HAS_HTTPX = False

BASE = "https://xsjy.whu.edu.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 列表接口的 city 参数收国标行政区划码，按地区精确过滤远胜关键词模糊搜索
# （踩过的坑：用「武汉」当 title 关键词搜，返回的全是名字里带"武汉"的教培公司）
CITY_CODES = {
    "武汉": "420100", "孝感": "420900", "襄阳": "420600", "宜昌": "420500",
    "黄石": "420200", "十堰": "420300", "荆州": "421000", "黄冈": "421100",
    "湖北": "420000",   # 全省兜底，覆盖上面没列到的地级市
}

# 地区内简章数量有限，再按技术词全国搜一轮，补充可远程/值得关注的岗位
TECH_KEYWORDS = ["数据开发", "人工智能", "算法工程师", "软件开发", "大数据"]

DETAIL_LIMIT = 230        # 最多拉多少条简章详情
CONCURRENCY = 10          # 详情并发数
PAGE_SIZE = 50
MAX_PAGE = 5              # 单个地区最多翻几页

# 简章正文里这些域名是真正的网申入口（企业自建/第三方招聘表单）
APPLY_HOST_HINTS = (
    "jsj.top", "wjx.cn", "wjx.top", "jinshuju", "zhaopin", "campus", "hr.",
    "job", "recruit", "talent", "career", "moka", "mokahr", "dayee", "beisen",
    "lietou", "app.mokahr", "hotjob", "ncss.cn",
)


def _pick_apply_url(content_html: str) -> str:
    """从简章正文里挑出最像「网申入口」的链接。

    关键：只认真正像表单/网申入口的域名（jsj.top、wjx、moka…）。
    若正文里找不到这类链接，返回空字符串 —— 交给调用方回退到「简章详情页」
    （武大就业网招聘简章页，本身就有完整岗位清单与投递方式），**绝不拿公司首页凑数**。
    这是「点过去就能投/看到简章」而不是「还要自己找」的根本保证。
    """
    if not content_html:
        return ""
    urls = re.findall(r'https?://[^\s\'"<>）)]{10,120}', content_html)
    for u in urls:
        low = u.lower()
        if any(k in low for k in APPLY_HOST_HINTS):
            return u.rstrip("。，,.、")
    return ""


def _clean_city(name: str) -> str:
    """'湖北省武汉市' -> '武汉'，便于与画像里的城市做包含匹配。"""
    if not name:
        return ""
    s = re.sub(r"^.*?省|^.*?自治区|市辖区", "", name)
    return s.replace("市", "").strip() or name


class WhuJobsSource(SourceAdapter):
    name = "武汉大学就业网"

    async def _token(self, cli) -> str:
        r = await cli.get(f"{BASE}/f/ajaxHome/getToken")
        return (r.json() or {}).get("data", "")

    async def _page(self, cli, token: str, page: int = 1,
                    city: str = "", keyword: str = "") -> tuple[list[dict], int]:
        """取一页简章列表。positionType=1 即全职（排除实习/兼职）。"""
        data = {"pageNo": page, "pageSize": PAGE_SIZE, "positionType": 1}
        if city:
            data["city"] = city
        if keyword:
            data["title"] = keyword
        try:
            r = await cli.post(
                f"{BASE}/f/recruitmentinfo/ajax_frontRecruitinfo",
                headers={"token": token}, data=data,
            )
            obj = (r.json() or {}).get("object") or {}
            return obj.get("list") or [], int(obj.get("totalPage") or 1)
        except Exception:  # noqa: BLE001 - 单次失败不影响整体
            return [], 1

    async def _collect_city(self, cli, token: str, code: str) -> list[dict]:
        """把某个地区的简章全部翻完（受 MAX_PAGE 限制）。"""
        first, total = await self._page(cli, token, 1, city=code)
        items = list(first)
        pages = min(total, MAX_PAGE)
        if pages > 1:
            rest = await asyncio.gather(*[
                self._page(cli, token, p, city=code) for p in range(2, pages + 1)
            ])
            for lst, _ in rest:
                items.extend(lst)
        return items

    async def _detail(self, cli, token: str, item: dict) -> list[Job]:
        """拉简章详情并展开为具体岗位。"""
        rid = item.get("id")
        if not rid:
            return []
        try:
            r = await cli.post(
                f"{BASE}/f/recruitmentinfo/ajax_show",
                headers={"token": token},
                data={"recruitmentId": rid},
            )
            info = ((r.json() or {}).get("object") or {}).get("recruitmentinfo") or {}
        except Exception:  # noqa: BLE001
            return []

        company = info.get("corporationName") or item.get("corporationName") or ""
        corp = info.get("corporationinfo") or {}
        nature = corp.get("corporationNatureValue") or ""
        deadline = (info.get("endTime") or item.get("endTime") or "")[:10]
        notice_title = info.get("title") or item.get("title") or ""
        # 简章详情页：始终可跳，且页面里就有岗位清单和投递方式
        notice_url = f"{BASE}/f/recruitmentinfo/show?recruitmentId={rid}"
        # 企业自己的网申入口：能拿到就用它，这才是「点了直接投」
        apply_url = _pick_apply_url(info.get("content") or "")

        url = apply_url or notice_url
        link_type = "官方网申" if apply_url else "简章详情"

        positions = info.get("recruitmentPositionList") or []
        jobs: list[Job] = []

        for p in positions:
            title = (p.get("positionName") or "").strip()
            if not title:
                continue
            jobs.append(Job(
                company=company,
                title=title,
                city=_clean_city(p.get("cityName") or ""),
                education=p.get("studentType") or info.get("education") or "",
                batch=notice_title,          # 交给 recognize_batch 归一化
                deadline=deadline,
                url=url,
                link_type=link_type,
                source=self.name,
                nature=nature,
                major=p.get("majorName") or "",
                salary=p.get("salaryMonth") or "",
            ))

        # 简章没挂具体岗位时，退化成一条「整份简章」，仍比公司首页有用
        if not jobs:
            jobs.append(Job(
                company=company,
                title=notice_title,
                city="",
                education=info.get("education") or "",
                batch=notice_title,
                deadline=deadline,
                url=url,
                link_type=link_type,
                source=self.name,
                nature=nature,
            ))
        return jobs

    async def fetch(self, profile) -> list[Job]:  # noqa: D102
        if not HAS_HTTPX:
            return []

        limits = httpx.Limits(max_connections=CONCURRENCY + 4)
        async with httpx.AsyncClient(
            timeout=25, verify=certifi.where(), follow_redirects=True, limits=limits,
            headers={"User-Agent": UA, "Referer": f"{BASE}/"},
        ) as cli:
            token = await self._token(cli)
            if not token:
                return []

            # 画像里的地区 → 行政区划码；用户改了地区，抓取范围自动跟着变
            regions = list(profile.get("regions") or []) + list(profile.get("cities") or [])
            codes = [CITY_CODES[r] for r in regions if r in CITY_CODES]
            if not codes:
                codes = [CITY_CODES["湖北"]]
            codes = list(dict.fromkeys(codes))

            geo_lists = await asyncio.gather(*[self._collect_city(cli, token, c) for c in codes])
            tech_pages = await asyncio.gather(*[
                self._page(cli, token, 1, keyword=k) for k in TECH_KEYWORDS
            ])

            # 交替取地域组 / 技术组，两边都能拿到详情配额
            def flatten(groups):
                out = []
                for lst in groups:
                    out.extend(lst)
                return out

            geo_items = flatten(geo_lists)
            tech_items = flatten([lst for lst, _ in tech_pages])
            ordered: list[dict] = []
            seen_ids: set[str] = set()
            for i in range(max(len(geo_items), len(tech_items))):
                for pool in (geo_items, tech_items):
                    if i < len(pool):
                        rid = pool[i].get("id")
                        if rid and rid not in seen_ids:
                            seen_ids.add(rid)
                            ordered.append(pool[i])

            targets = ordered[:DETAIL_LIMIT]
            sem = asyncio.Semaphore(CONCURRENCY)

            async def guarded(it):
                async with sem:
                    return await self._detail(cli, token, it)

            results = await asyncio.gather(*[guarded(it) for it in targets],
                                           return_exceptions=True)

        jobs: list[Job] = []
        for r in results:
            if isinstance(r, list):
                jobs.extend(r)
        return jobs
