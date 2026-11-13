"""湖北高校就业网络联盟（91wllm.cn）多校采集源 —— 把岗位池拉到与用户段位对齐。

为什么接它（结合画像：2027 数据科学 / 民办二本 / 武汉·孝感·湖北 / AI应用·数据开发）：
  * 原有 6 源全在「高门槛赛道」：武大、华科是 985 就业网，国聘是央国企总部，
    实习僧偏实习 —— 岗位池天然为硕博与名校准备，民办本科可投率低。
  * 91wllm.cn 是湖北省教育厅「湖北 24365 大学生就业服务平台 / 湖北高校就业网络联盟」，
    全省高校就业网都是它的子域名、模板完全一致：
        hkxy    汉口学院（民办）              wtbu   武汉工商学院（民办）
        wsyu    武昌首义学院（民办）           hbc    湖北商贸学院（民办）
        witptjy 武汉工程大学邮电与信息工程学院（独立学院）
        wit     武汉工程大学（双非）           hbeu   湖北工程学院（孝感）
    这些站上的企业是明确面向双非/民办招人的 —— 正是用户的真实主战场。
  * 一套解析器 = 一次接入覆盖十几所湖北高校的就业市场，这是其它源给不了的。

接口（逐条实测，无需登录，2026-09-21 验证）：
  GET /                          首页，服务端渲染，含四块「最新」数据
      /campus/view/id/{id}       招聘公告（简章）
      /job/view/id/{id}          职位详情（含单位性质 / 行业 / 规模）
      /teachin/view/id/{id}      宣讲会（字段太少，暂未使用）
  * 简章详情页内嵌岗位表 —— 富矿：**一次请求换 N 条岗位**，每行含
    岗位名 / 薪资 / 城市 / 全职实习 / 学历 / 需求专业，且岗位名自带
    /job/view/id/{id} 直链（点过去就是职位详情 + 投递入口）。
  * 反例（不要走，白费请求，实测过的坑）：
    - /job/index     有登录墙，302 -> /user/login
    - /job/search    纯搜索表单页，列表由 JS 拉取，服务端不下发数据
    - /campus/index  同上，是搜索页而非列表页
    - 职位 / 简章首页第一段请求即可，不要遍历 id 区间（属硬爬，违规）

抓取策略（控制请求量，优先对方站点体验）：
  1. 每校只抓首页这个「最新」窗口（简章 / 职位各取前 N 条），不做全站遍历
  2. 主体走简章详情（1 请求 = N 岗位，性价比最高）
  3. 少量职位进详情，补「单位性质 / 行业」——简章表格没有这几列，
     而 enterprise_type 正是看板的筛选维度
  4. 全局限并发 + 连续失败熔断；单校异常不影响其它校
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import certifi

from ..models import Job
from .base import SourceAdapter
from .whu_jobs import _clean_city

logger = logging.getLogger(__name__)

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - 环境缺依赖时整个源降级为空
    HAS_HTTPX = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 平台子域名 -> 校名。顺序即优先级：民办 / 独立学院打头（用户段位最匹配），
# 再武汉双非，最后地方院校（孝感 / 宜昌 / 黄石等，补非武汉的湖北岗）。
SCHOOLS: tuple[tuple[str, str], ...] = (
    ("hkxy", "汉口学院"),
    ("wtbu", "武汉工商学院"),
    ("wsyu", "武昌首义学院"),
    ("hbc", "湖北商贸学院"),
    ("witptjy", "武汉工程大学邮电与信息工程学院"),
    ("whicu", "武汉商贸职业学院"),
    ("wit", "武汉工程大学"),
    ("whpu", "武汉轻工大学"),
    ("jhun", "江汉大学"),
    ("wtu", "武汉纺织大学"),
    ("hbut", "湖北工业大学"),
    ("hbue", "湖北经济学院"),
    ("wust", "武汉科技大学"),
    ("hbeu", "湖北工程学院"),
)

MAX_CAMPUS_PER_SCHOOL = 16   # 每校取前 N 份简章进详情（首页最新窗口，实测 8~20 条）
MAX_JOB_PER_SCHOOL = 5       # 每校再补 N 条职位详情（拿单位性质 / 行业）
CONCURRENCY = 6              # 全局并发上限
DETAIL_GAP = 0.15            # 详情请求之间的小间隔，避免顶住对方站点

# 熔断：连续失败达阈值即短路，冷却期内整源跳过，避免对着挂掉的站点狂发请求
FAIL_THRESHOLD = 8
COOLDOWN_SEC = 600

_fail_streak = 0
_open_until = 0.0


def _breaker_open() -> bool:
    return time.time() < _open_until


def _record_fail(where: str = "") -> None:
    global _fail_streak, _open_until
    _fail_streak += 1
    if _fail_streak >= FAIL_THRESHOLD and not _breaker_open():
        _open_until = time.time() + COOLDOWN_SEC
        logger.warning(
            f"[91wllm源] 连续 {_fail_streak} 次失败（{where}），熔断 {COOLDOWN_SEC}s，"
            f"冷却期内跳过该源")


def _record_ok() -> None:
    global _fail_streak, _open_until
    _fail_streak = 0
    _open_until = 0.0


# ── 文本清洗 ────────────────────────────────────────────────
_ENT = (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&ldquo;", "“"), ("&rdquo;", "”"))


def _t(s: str) -> str:
    """去标签 + 解实体 + 压空白。"""
    s = re.sub(r"<[^>]+>", " ", s or "")
    for a, b in _ENT:
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _clean_title(raw: str) -> str:
    """去掉简章标题的「[校外转载]」标注。

    源站用它区分「本校专场 / 转载的校外岗」。对投递没有意义，却会挤掉标题里
    真正有信息量的部分（岗位名/企业名），所以在入库前剥掉。
    """
    return re.sub(r"^\s*[\[【(（]?\s*校外转载\s*[\]】)）]?\s*", "", raw or "").strip()


def _clean_place(raw: str) -> str:
    """把地点归一成城市名。

    简章表格给的是 '湖北省 - 襄阳市' 这种「省 - 市」拼接，职位详情给的是
    '湖南省长沙市天心区' 这种整串。两种都要能吃：
      '湖北省 - 襄阳市'   -> 襄阳   （取末段）
      '北京市 - 朝阳区'   -> 北京   （末段是区县则回退到首段）
      '湖南省长沙市天心区' -> 长沙   （交给 _clean_city 逐层剥离）
    """
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"\s*[-–—]\s*", raw) if p.strip()]
    if len(parts) >= 2:
        tail = _clean_city(parts[-1])
        if tail.endswith(("区", "县")):
            head = _clean_city(parts[0])
            if head and not head.endswith(("区", "县")):
                return head
        return tail
    return _clean_city(raw)


# ── 页面解析 ────────────────────────────────────────────────
_RE_IDS = {
    "campus": re.compile(r"/campus/view/id/(\d+)"),
    "job": re.compile(r"/job/view/id/(\d+)"),
}

# 简章页头部
_RE_NOTICE_TITLE = re.compile(r'<div class="title-message">.*?<h5>(.*?)</h5>', re.S)
_RE_NOTICE_COMPANY = re.compile(
    r'<a[^>]*class="name[^"]*"[^>]*href="/company/view/id/\d+"[^>]*>(.*?)</a>', re.S)
_RE_EXPIRE = re.compile(r'<span class="expired_time">\s*过期时间[：:]\s*'
                        r'(\d{4}-\d{2}-\d{2})')

# 简章内嵌岗位表：<tr> 里 1 个岗位链接 + 4 个 <li>（薪资/城市/类型/学历）+ 需求专业列
_RE_TABLE = re.compile(r"<table.*?</table>", re.S)
_RE_TR = re.compile(r"<tr.*?</tr>", re.S)
_RE_ROW_JOB = re.compile(r'<a[^>]*title="([^"]+)"[^>]*href="/job/view/id/(\d+)')
_RE_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_RE_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)

# 职位详情页
_RE_JOB_TITLE = re.compile(r'<span class="title"[^>]*title="([^"]*)"')
_RE_JOB_SALARY = re.compile(r'<span class="text-orange salary">(.*?)</span>', re.S)
_RE_JOB_EDU = re.compile(r'<span class="education">(.*?)</span>', re.S)
_RE_JOB_COMPANY = re.compile(
    r'<div class="unit-info">.*?<a href="/company/view/id/\d+"[^>]*>(.*?)</a>', re.S)


def _label(html: str, name: str) -> str:
    """取职位详情侧栏 <label>单位性质：</label><span>值</span> 里的值。"""
    m = re.search(r'<label class="label">\s*' + name + r'[：:]\s*</label>\s*'
                  r'<span>(.*?)</span>', html, re.S)
    return _t(m.group(1)) if m else ""


def _parse_notice(html: str, base: str, notice_id: str, school: str) -> list[Job]:
    """简章详情 -> 岗位列表。岗位表缺失时退化成一条「整份简章」。"""
    m = _RE_NOTICE_TITLE.search(html)
    notice_title = _clean_title(_t(m.group(1))) if m else ""
    m = _RE_NOTICE_COMPANY.search(html)
    company = _t(m.group(1)) if m else ""
    m = _RE_EXPIRE.search(html)
    deadline = m.group(1) if m else ""

    jobs: list[Job] = []
    mt = _RE_TABLE.search(html)
    if mt:
        for row in _RE_TR.findall(mt.group(0)):
            rj = _RE_ROW_JOB.search(row)
            if not rj:
                continue
            title = _t(rj.group(1))
            if not title:
                continue
            lis = [_t(x) for x in _RE_LI.findall(row)]
            salary = lis[0] if len(lis) > 0 else ""
            city = _clean_place(lis[1] if len(lis) > 1 else "")
            jtype = lis[2] if len(lis) > 2 else ""
            edu = lis[3] if len(lis) > 3 else ""
            tds = _RE_TD.findall(row)
            major = _t(tds[2]) if len(tds) >= 3 else ""
            jobs.append(Job(
                company=company or notice_title,
                title=title,
                city=city,
                education=edu,
                # 具体岗位的「全职/实习」比简章标题更准，实习岗单独归批
                batch="实习" if "实习" in jtype else notice_title,
                deadline=deadline,
                url=f"{base}/job/view/id/{rj.group(2)}",
                link_type="简章详情",       # 源站岗位详情页，直连投递入口
                source="湖北高校就业网",
                major=major,
                salary=salary,
                jd_text=f"{notice_title} {school} {jtype}",
            ))

    if not jobs and (company or notice_title):
        jobs.append(Job(
            company=company or notice_title,
            title=notice_title,
            deadline=deadline,
            url=f"{base}/campus/view/id/{notice_id}",
            link_type="简章详情",
            source="湖北高校就业网",
            batch=notice_title,
            jd_text=f"{notice_title} {school}",
        ))
    return jobs


def _parse_job(html: str, base: str, job_id: str, school: str) -> Job | None:
    """职位详情 -> 单条岗位（补上简章表格没有的单位性质 / 行业）。"""
    m = _RE_JOB_TITLE.search(html)
    title = _t(m.group(1)) if m else ""
    if not title:
        return None
    m = _RE_JOB_SALARY.search(html)
    salary = _t(m.group(1)) if m else ""
    # <span class="education"> | 城市 | 全职 | 本科 </span>
    m = _RE_JOB_EDU.search(html)
    bits = [b.strip() for b in _t(m.group(1)).split("|") if b.strip()] if m else []
    city = _clean_place(bits[0]) if bits else ""
    jtype = bits[1] if len(bits) > 1 else ""
    edu = bits[2] if len(bits) > 2 else ""

    m = _RE_JOB_COMPANY.search(html)
    company = _t(m.group(1)) if m else ""
    if not company:
        return None   # 没公司名的岗位没法投，直接丢

    return Job(
        company=company,
        title=title,
        city=city,
        education=edu,
        batch="实习" if "实习" in jtype else "",
        url=f"{base}/job/view/id/{job_id}",
        link_type="简章详情",
        source="湖北高校就业网",
        nature=_label(html, "单位性质"),
        industry=_label(html, "单位行业"),
        major=_label(html, "需求专业"),
        salary=salary,
        jd_text=f"{title} {school} {jtype}",
    )


class Hbbys91wllmSource(SourceAdapter):
    """湖北高校就业网络联盟多校站。"""

    name = "湖北高校就业网"

    async def _get(self, cli, url: str) -> str:
        try:
            r = await cli.get(url)
            if r.status_code >= 400:
                _record_fail(f"{r.status_code} {url[-40:]}")
                return ""
            return r.text
        except Exception:  # noqa: BLE001 - 单次失败不影响整体
            _record_fail(f"ERR {url[-40:]}")
            return ""

    async def _detail(self, cli, base: str, kind: str, item_id: str,
                      school: str) -> list[Job]:
        path = "campus/view" if kind == "campus" else "job/view"
        html = await self._get(cli, f"{base}/{path}/id/{item_id}")
        if not html:
            return []
        _record_ok()
        try:
            if kind == "campus":
                return _parse_notice(html, base, item_id, school)
            j = _parse_job(html, base, item_id, school)
            return [j] if j else []
        except Exception:  # noqa: BLE001 - 单页解析失败不影响其它
            logger.debug("[91wllm源] 解析失败 %s/%s", kind, item_id)
            return []

    async def fetch(self, profile) -> list[Job]:  # noqa: D102
        if not HAS_HTTPX:
            return []
        if _breaker_open():
            logger.info("[91wllm源] 熔断冷却中，本次扫描跳过该源")
            return []

        sem = asyncio.Semaphore(CONCURRENCY)

        async def guarded(coro_factory):
            async with sem:
                await asyncio.sleep(DETAIL_GAP)
                return await coro_factory()

        limits = httpx.Limits(max_connections=CONCURRENCY + 4)
        async with httpx.AsyncClient(
            timeout=25, verify=certifi.where(), follow_redirects=True, limits=limits,
            headers={"User-Agent": UA},
        ) as cli:
            # 1) 并发抓各校首页，取「最新」窗口里的简章 / 职位 id
            homes = await asyncio.gather(*[
                guarded(lambda sub=sub: self._get(cli, f"https://{sub}.91wllm.cn/"))
                for sub, _ in SCHOOLS
            ], return_exceptions=True)

            targets: list[tuple[str, str, str, str]] = []
            seen: set = set()
            for (sub, school), html in zip(SCHOOLS, homes):
                if not isinstance(html, str) or not html:
                    continue
                base = f"https://{sub}.91wllm.cn"
                for kind, cap in (("campus", MAX_CAMPUS_PER_SCHOOL),
                                  ("job", MAX_JOB_PER_SCHOOL)):
                    ids = list(dict.fromkeys(_RE_IDS[kind].findall(html)))[:cap]
                    for i in ids:
                        key = (base, kind, i)
                        if key in seen:
                            continue
                        seen.add(key)
                        targets.append((base, kind, i, school))

            if not targets:
                return []

            # 2) 并发拉详情。熔断后剩余任务直接短路，不再发请求
            async def one(b: str, k: str, i: str, sc: str) -> list[Job]:
                if _breaker_open():
                    return []
                return await self._detail(cli, b, k, i, sc)

            results = await asyncio.gather(*[
                guarded(lambda b=b, k=k, i=i, sc=sc: one(b, k, i, sc))
                for b, k, i, sc in targets
            ], return_exceptions=True)

        jobs: list[Job] = []
        for r in results:
            if isinstance(r, list):
                jobs.extend(r)
        logger.info("[91wllm源] %d 个详情页 -> %d 条岗位", len(targets), len(jobs))
        return jobs
