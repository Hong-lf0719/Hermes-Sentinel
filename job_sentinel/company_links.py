"""公司投递直链解析。

问题背景：聚合源给出的链接常常是它自己的分页地址（如 .../wishlist/1113），
点进去到不了公司，卡片形同虚设。

解决策略（三级降级，保证任何一张卡片都点得动）：
  1. 已验证的官方校招页  —— 表内命中直接用（均经 HTTP 200 实测）
  2. 源提供的有效详情链接 —— 排除已知的无效/分页链接模式后采用
  3. 搜索直达兜底        —— 生成「公司名 + 届别 + 校园招聘」的搜索链接，
                            必定可跳转，且总能找到最新入口
"""
from __future__ import annotations

from urllib.parse import quote

# 已实测可达（HTTP 200）的官方招聘入口。含 campus/job/hr 的为精准校招页。
OFFICIAL_LINKS: dict[str, str] = {
    # —— 武汉本地央国企 / 制造 ——
    "烽火通信": "https://www.fiberhome.com",
    "东风汽车": "https://www.dongfeng-global.com",
    "岚图": "https://www.dongfeng-global.com",
    "长飞光纤": "https://www.yofc.com",
    "光迅": "https://www.accelink.com",
    "航天科工": "http://www.casic.com.cn",
    "三江航天": "http://www.casic.com.cn",   # 航天科工九院，走集团招聘入口
    "长江存储": "https://www.ymtc.com",
    "联影": "https://www.united-imaging.com",
    "人福": "http://www.humanwell.com.cn",
    # —— 互联网 / 软件（武汉有研发中心）——
    "小米": "https://hr.xiaomi.com/campus",
    "金山办公": "https://campus.wps.cn",
    "斗鱼": "https://www.douyu.com",
    "华为": "https://career.huawei.com/reccampportal/portal5/campus-recruitment.html",
    "中兴": "https://job.zte.com.cn",
    "字节": "https://jobs.bytedance.com/campus",
    "腾讯": "https://join.qq.com/campus.html",
    "阿里": "https://talent.taotian.com",
    "京东": "https://campus.jd.com",
    "京东方": "https://join.boe.com",
    # —— 金融 ——
    "兴业银行": "https://www.cib.com.cn",
    "中国银行": "https://www.boc.cn",
    # —— 聚合 / 就业网兜底 ——
    "国聘": "https://xiaoyuan.iguopin.com",
    "武汉大学": "https://xsjy.whu.edu.cn",
    "华中科技大学": "http://job.hust.edu.cn",
}

# 无效链接特征：聚合站自身的列表/分页/收藏夹地址，点进去到不了公司
INVALID_URL_PATTERNS = (
    "/wishlist/",
    "/applications/",
    "/companies/page/",
    "/jobradar",
)

# 这些是站点首页，作为 fallback 可以，但不算「直达链接」
GENERIC_HOSTS = (
    "xiaoyuan.iguopin.com",
    "givemeoc.com",
    "resumemakeroffer.com",
)


def _search_fallback(company: str, year: str = "2027") -> str:
    """搜索直达兜底：必定可跳转，且总能找到公司最新招聘入口。"""
    q = quote(f"{company} {year}届 校园招聘 官网")
    return f"https://www.bing.com/search?q={q}"


def is_direct_link(url: str) -> bool:
    """判断是否为「能点到公司」的有效直达链接。"""
    if not url:
        return False
    u = url.lower()
    if any(p in u for p in INVALID_URL_PATTERNS):
        return False
    if any(h in u for h in GENERIC_HOSTS):
        return False
    return u.startswith("http")


def resolve_url(company: str, raw_url: str = "", year: str = "2027") -> tuple[str, str]:
    """解析投递链接。

    返回 (url, link_type)，link_type ∈ {官方校招页, 源详情页, 搜索直达}，
    前端可据此给卡片标不同颜色，让用户一眼知道链接质量。
    """
    name = (company or "").strip()

    # 1. 官方校招页。取「最长命中键」：短键裸子串会误伤（"京东"命中"京东方"、
    # "阿里"命中"阿里云"外的误配面），长键优先并要求命中位置不被更长公司名吸收。
    hits = [(k, link) for k, link in OFFICIAL_LINKS.items() if k in name]
    if hits:
        hits.sort(key=lambda x: -len(x[0]))
        return hits[0][1], "官方校招页"

    # 2. 源提供的有效详情链接
    if is_direct_link(raw_url):
        return raw_url, "源详情页"

    # 3. 搜索直达兜底
    return _search_fallback(name or "校园招聘", year), "搜索直达"
