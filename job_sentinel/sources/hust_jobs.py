"""华中科技大学就业信息网采集源 —— 武汉本地校招补充源。

为什么接它（结合画像：2027 数据科学 / 武汉·孝感 / AI应用·数据开发）：
  * 华科是武汉 CS/AI 最强校，其就业网岗位高度本地化，与武大就业网互补
  * 列表页服务端渲染、公开、无需登录；分页规律稳定（searchJob.do / searchJob_N.do）
  * 实测可达：job.hust.edu.cn（单数）能正常返回 200，列表结构清晰

接口（已实测，无需登录）：
  GET  /searchJob.do?type=2&fbsj=           -> 招聘信息(校招) 第 1 页
  GET  /searchJob_2.do?type=2&fbsj=         -> 第 2 页 …… searchJob_N.do
  列表条目在 <ul class="zplist wow"> 的 <li> 内：
      [招聘信息] <a href=".../zpinfo1/XXXX.htm" title="...">标题</a>
      <span class="n2">[2026-08-16]</span>
  type 取值：2=招聘信息(校招)  4=实习生信息

抓取策略（控制请求量，安全优先）：
  * 只抓最新若干页（MAX_PAGES），不做全量 40054 条历史
  * 列表页无城市字段；华科是本校门户、发布地即武汉，故 city 默认「武汉」
    （符合「全量入库 + 地区聚焦」设计：用户用「只看目标地区」+「标记不投」手工聚焦）
  * 不进详情页解析（详情是 Word 生成的脏 HTML，城市/学历抽取脆弱且需大量请求）
  * 任何单页失败都返回已拿到的结果，不影响主流程
"""
from __future__ import annotations

import asyncio
import re
import html as ihtml

import certifi
from ..models import Job
from .base import SourceAdapter

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover
    HAS_HTTPX = False

BASE = "https://job.hust.edu.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# type=2 招聘信息(校招) 是主渠道；type=4 实习生可后续补
LIST_TYPES = ["2"]
MAX_PAGES = 5          # 每类最多翻几页（最新 ~75 条）
CONCURRENCY = 4
TIMEOUT = 20

# 列表 ul 内的单条条目：标题链接 + 详情 URL + 发布日期
# 注意：服务端返回的 href 可能是相对路径 /zpinfo1/XXXX.htm（httpx/curl 直连），
#       也可能是浏览器保存的绝对路径（https://job.hust.edu.cn/zpinfo1/XXXX.htm），
#       两者都要匹配，再统一拼成绝对 URL。
_ITEM_RE = re.compile(
    r'href="(?:https?://job\.hust\.edu\.cn)?/zpinfo1/(\d+\.htm)"[^>]*title="([^"]*)"',
    re.S,
)
_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
_BLOCK_RE = re.compile(r'class="zplist[^"]*">(.*?)</ul>', re.S)


def _list_url(page: int, jtype: str) -> str:
    if page <= 1:
        return f"{BASE}/searchJob.do?type={jtype}&fbsj="
    return f"{BASE}/searchJob_{page}.do?type={jtype}&fbsj="


def _parse_list(html: str) -> list[tuple[str, str, str]]:
    """返回 [(detail_url, title, date_str)]。"""
    out: list[tuple[str, str, str]] = []
    m = _BLOCK_RE.search(html)
    if not m:
        return out
    block = m.group(1)
    # 逐条 li 切分，避免跨条目串味
    for chunk in block.split("<li"):
        if "zpinfo1" not in chunk:
            continue
        im = _ITEM_RE.search(chunk)
        if not im:
            continue
        zid, title = im.group(1), ihtml.unescape(im.group(2)).strip()
        url = f"{BASE}/zpinfo1/{zid}"
        if not title:
            continue
        dm = _DATE_RE.search(chunk)
        date = dm.group(1) if dm else ""
        out.append((url, title, date))
    return out


class HustJobsSource(SourceAdapter):
    name = "华中科技大学就业网"

    async def _fetch_page(self, cli, page: int, jtype: str) -> list[tuple[str, str, str]]:
        try:
            r = await cli.get(_list_url(page, jtype))
            if r.status_code != 200:
                return []
            return _parse_list(r.text)
        except Exception:  # noqa: BLE001 - 单页失败不影响整轮
            return []

    async def fetch(self, profile) -> list[Job]:  # noqa: D102
        if not HAS_HTTPX:
            return []

        limits = httpx.Limits(max_connections=CONCURRENCY + 2)
        jobs: list[Job] = []
        seen: set[str] = set()

        async with httpx.AsyncClient(
            timeout=TIMEOUT, verify=certifi.where(), follow_redirects=True, limits=limits,
            headers={"User-Agent": UA, "Referer": f"{BASE}/"},
        ) as cli:
            for jtype in LIST_TYPES:
                for page in range(1, MAX_PAGES + 1):
                    items = await self._fetch_page(cli, page, jtype)
                    if not items:
                        break  # 该类型已到末页或无数据
                    for url, title, date in items:
                        key = f"{title}|{url}"
                        if key in seen:
                            continue
                        seen.add(key)
                        jobs.append(Job(
                            company="",                  # 列表仅给标题，公司名含在标题里；不瞎猜
                            title=title,
                            city="武汉",                 # 华科本校门户，发布地即武汉（地区聚焦用）
                            education="",                # 留空 → 打分时 suggest_fill 推「建议方向」
                            batch="",                    # 留空 → recognize_batch 按标题推断
                            deadline="",
                            url=url,
                            link_type="简章详情",
                            source=self.name,
                            nature="",
                            industry="",
                            major="",
                            first_seen=date,            # 用发布日作首见日，便于「最新发现」排序
                        ))
        return jobs
