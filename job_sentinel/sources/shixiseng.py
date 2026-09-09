"""实习僧采集源 —— 外企/数据向实习岗位补充。

为什么加这个源：
  * 用户画像（武汉·孝感 / 数据科学 / 外企投递缺口）下，国聘外企仅 37 条且武汉 2 条，
    现有源（武大/华科/国聘/offer多多）外企密度极低。实习僧外企（尤其 500 强管培/
    数据分析/软件开发）密集，是低成本补外企供给的优质渠道。
  * 实习僧列表是服务端渲染 HTML（无干净 JSON 接口），逐卡片正则解析。
  * 注意：实习僧卡片**不直接标企业性质**，外企靠「公司名启发式 + 卡片 500强/外资 文本」
    识别，命中才标 enterprise_type=外企，未命中留空（看板可按外企筛选）。
  * 实习僧主力是实习岗，profile.batches 已含「实习」，可正常过 hard_filter。
"""
from __future__ import annotations

import asyncio
import re
from html import unescape

try:
    import httpx
except ImportError:  # 缺依赖时静默降级（与其它源一致），不让整个 sources 注册表炸掉
    httpx = None

try:
    import certifi
    _VERIFY = certifi.where()
except Exception:  # pragma: no cover
    _VERIFY = True

from ..models import Job
from .base import SourceAdapter

BASE = "https://www.shixiseng.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 目标城市（实习僧 city 参数用中文城市名）
CITIES = ("武汉", "孝感", "湖北")

# 方向关键词 —— 贴合画像（数据科学 / AI 应用开发 / 数据开发）
KEYWORDS = (
    "数据", "数据分析", "数据开发", "人工智能", "机器学习",
    "算法", "Python", "Java", "软件开发", "大数据",
)

MAX_PAGE = 5          # 单关键词×城市最多翻几页
PAGE_DELAY = 0.6      # 页间间隔（秒），友好抓取

# 外企公司名启发式词表（命中即标外企）。覆盖常见 500 强 / 跨国 / 外资品牌。
_FOREIGN_HINTS = (
    "微软", "IBM", "埃森哲", "SAP", "西门子", "思爱普", "惠普", "HP", "英特尔", "Intel",
    "戴尔", "Dell", "思科", "Cisco", "甲骨文", "Oracle", "谷歌", "Google", "亚马逊", "Amazon",
    "苹果", "Apple", "Meta", "脸书", "Facebook", "英伟达", "NVIDIA", "高通", "Qualcomm",
    "辉瑞", "Pfizer", "宝洁", "P&G", "联合利华", "Unilever", "雀巢", "Nestle", "可口可乐",
    "奔驰", "梅赛德斯", "宝马", "BMW", "大众", "Volkswagen", "奥迪", "丰田", "Toyota",
    "本田", "Honda", "日产", "尼桑", "松下", "Panasonic", "索尼", "Sony", "三星", "Samsung",
    "LG", "现代", "霍尼韦尔", "Honeywell", "施耐德", "Schneider", "ABB", "飞利浦", "Philips",
    "欧莱雅", "L'Oréal", "百事", "玛氏", "江森", "罗氏", "诺华", "Novartis", "拜耳", "Bayer",
    "赛诺菲", "Sanofi", "强生", "Johnson", "礼来", "GE", "通用电气", "空客", "Airbus",
    "波音", "Boeing", "特斯拉", "Tesla", "领英", "LinkedIn", "优步", "Uber", "爱立信",
    "爱普生", "佳能", "Canon", "理光", "富士通", "Fujitsu", "NEC", "东芝", "Toshiba",
    "资生堂", "雅诗兰黛", "Estee", "肯德基", "KFC", "麦当劳", "必胜客", "宜家", "IKEA",
    "米其林", "汇丰", "HSBC", "花旗", "Citibank", "渣打", "Standard Chartered", "摩根",
    "德勤", "Deloitte", "普华永道", "PwC", "毕马威", "KPMG", "安永", "EY", "埃森哲",
    "育碧", "Ubisoft", "暴雪", "Blizzard", "推特", "Twitter",
)

# 卡片段正则
_RE_URL_TITLE = re.compile(
    r'href="(https://www\.shixiseng\.com/intern/[^"]+)"\s+title="([^"]*)"', re.S)
_RE_CITY = re.compile(r'<span class="city ellipsis"[^>]*>([^<]*)')
_RE_COMPANY = re.compile(
    r'class="f-r intern-detail__company"[^>]*>.*?<a title="([^"]+)"', re.S)

# 图标字体实体（如 &#xee6b 或 &#xee6b;）不是真实文字，解析后清掉
_ENTITY_RE = re.compile(r"&#x?[0-9a-fA-F]+;?")
_PAREN_RE = re.compile(r"[（(][）)\s]*[）)]")

# 民企/中概否定词表：命中直接判非外企，压住卡片内「外企」推广文案的误标
# （如快手/字节卡片可能带平台级「外企在招」标签，导致误判）
_DOMESTIC_HINTS = (
    "字节", "ByteDance", "快手", "腾讯", "阿里", "阿里巴巴", "华为", "百度", "京东",
    "美团", "拼多多", "滴滴", "小米", "蔚来", "NIO", "理想", "小鹏", "大疆", "商汤",
    "旷视", "OPPO", "VIVO", "vivo", "网易", "携程", "哔哩哔哩", "B站", "蚂蚁",
    "滴滴出行", "唯品会", "搜狐", "新浪", "奇虎", "360", "字节跳动", "得物",
    "小红书", "作业帮", "好未来", "猿辅导", "贝壳", "贝壳找房", "满帮", "微众银行",
)


def _clean_text(s: str) -> str:
    if not s:
        return ""
    # 1) 解双层转义：页面把图标实体存成 &amp;#xee6b，先变回 &#xee6b
    s = s.replace("&amp;", "&")
    # 2) 删图标实体（在 unescape 前，否则变 PUA 字符无法识别）
    s = _ENTITY_RE.sub("", s)
    # 3) 解剩余 HTML 实体（如 &（可转正））
    s = unescape(s)
    s = _PAREN_RE.sub("", s)         # 去空括号（如 "数据开发()"）
    return re.sub(r"\s+", "", s).strip()


def _is_foreign(company: str, chunk: str) -> bool:
    """外企判定：卡片文本含 外企/外资/跨国 等明确标记，或公司名命中启发词。

    注意：不用「500强」判定——华为/字节/腾讯等民企也是 500 强，会误标外企。
    只用「外企 / 外资企业 / 外商独资 / 跨国公司」等明确外资语义词。
    民企/中概否定词最先判（压住卡片内「外企」推广文案误标）。
    """
    # 民企/中概否定词优先：命中直接非外企
    if company and any(h.lower() in company.lower() for h in _DOMESTIC_HINTS):
        return False
    if any(k in chunk for k in ("外企", "外资企业", "外商独资", "跨国公司", "外资")):
        return True
    if not company:
        return False
    # 英文短键按词边界匹配：裸子串会让 "GE" 命中 LenovE Group、"EY" 命中 SurveY、"现代" 命中 XX现代化
    low = company.lower()
    for h in _FOREIGN_HINTS:
        hl = h.lower()
        if hl.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(hl)}(?![a-z0-9])", low):
                return True
        elif h in ("现代",):  # 常用中文词，必须带后缀才算（现代汽车/现代集团）
            if "现代汽车" in company or "现代集团" in company:
                return True
        elif h in company:
            return True
    return False


def _parse_cards(html: str) -> list[Job]:
    jobs: list[Job] = []
    # 以每个卡片块为单位解析（intern-wrap 是卡片容器；注意 class 非首属性，
    # 不能锚 <div，按 class="intern-wrap 切分，避免整页当成一块）
    for chunk in re.split(r'(?=class="intern-wrap)', html):
        if 'intern-detail__job' not in chunk:
            continue
        m = _RE_URL_TITLE.search(chunk)
        if not m:
            continue
        url = m.group(1)
        title = _clean_text(m.group(2))
        if not title:
            continue
        city = _clean_text(_RE_CITY.search(chunk).group(1)) if _RE_CITY.search(chunk) else ""
        cm = _RE_COMPANY.search(chunk)
        company = _clean_text(cm.group(1)) if cm else ""
        if not company:
            continue
        is_foreign = _is_foreign(company, chunk)
        jobs.append(Job(
            company=company,
            title=title,
            city=city,
            batch="实习",
            url=url,
            link_type="源详情页",
            source="实习僧",
            enterprise_type="外企" if is_foreign else "",
            nature="外资企业" if is_foreign else "",
        ))
    return jobs


class ShixisengSource(SourceAdapter):
    name = "实习僧"

    async def fetch(self, profile: dict) -> list[Job]:
        """返回实习僧采集到的外企/数据向实习岗位。"""
        if not httpx:
            return []

        out: list[Job] = []
        headers = {"User-Agent": UA, "Referer": f"{BASE}/"}
        async with httpx.AsyncClient(
            timeout=25, verify=_VERIFY, follow_redirects=True, headers=headers,
        ) as cli:
            for city in CITIES:
                for kw in KEYWORDS:
                    for page in range(1, MAX_PAGE + 1):
                        url = f"{BASE}/interns"
                        params = {"k": kw, "c": city, "p": page}
                        try:
                            r = await cli.get(url, params=params, timeout=25)
                            r.raise_for_status()
                        except Exception:
                            break
                        cards = _parse_cards(r.text)
                        if not cards:
                            break  # 该关键词/城市翻完
                        out.extend(cards)
                        if len(cards) < 15:
                            break  # 末页
                        await asyncio.sleep(PAGE_DELAY)
        return out
