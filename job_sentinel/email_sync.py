"""投递邮件自动解析 —— 把智联/BOSS/企业聘用等邮件解析成岗位状态变更，回写 jobs 库。

两条接入路径（都在 server.py process_chat 触发）：
  1) QQ 邮箱设置「自动转发」把投递/笔试/面试/offer 邮件转发到 Hermes 的 Caspian 地址，
     转发邮件到达即被识别为投递邮件 → 解析 → 回写 → 回推汇总到手机。
  2) 用户把邮件正文粘贴进 Hermes 聊天，或说「解析投递邮件」+ 内容，同样解析回写。

纯解析逻辑，不依赖网络/凭据，可单测。
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 漏斗顺序：数值越大越靠后；挂=-1 表示终止态（可覆盖任意前置态）
STATUS_RANK = {"新": 0, "已看": 1, "已投": 2, "笔试": 3, "面试": 4, "OC": 5, "挂": -1}

# 状态抽取关键词（顺序即优先级：OC > 面试 > 笔试 > 已投 > 挂）。
# 挂放最后，且只用「明确否定」词；「感谢您关注」这类客套结尾不算拒信，避免误判。
STATUS_PATTERNS = [
    ("OC", ["录用通知", "录用函", "offer letter", "offer发放", "发放offer", "拟录用",
            "恭喜您被录用", "入职邀请", "已发offer", "offer!"]),
    ("面试", ["面试邀请", "面试通知", "邀您面试", "面试邀约", "一面", "二面", "三面", "终面",
              "一面通知", "二面通知", "终面通知", "技术面", "hr面", "业务面", "加面",
              "电话面试", "视频面试", "现场面试", "面试安排", "群面通知"]),
    ("笔试", ["笔试邀请", "笔试通知", "在线笔试", "笔试链接", "在线测评", "测评邀请",
              "笔试安排", "机考通知", "笔试题"]),
    ("已投", ["投递成功", "简历已收到", "简历已查收", "投递完成", "已成功投递",
              "投递申请已提交", "网申已提交", "投递确认", "已投递", "简历接收"]),
    ("挂", ["未通过", "不合适", "很遗憾", "未进入", "婉拒", "落选", "未录用",
            "暂不匹配", "申请未通过", "未能进入", "不予录用"]),
]

# 强信号：出现其一即可判定为投递邮件（降低普通聊天的误触发）
DELIVERY_STRONG = set()
for _s, _kws in STATUS_PATTERNS:
    for _k in _kws:
        DELIVERY_STRONG.add(_k.lower())

# 平台/渠道标记
PLATFORM_MARKERS = ["智联", "boss直聘", "猎聘", "前程无忧", "51job", "校招", "网申",
                    "招聘", "hr", "offer", "校招网", "投递"]

# 常见公司别名（邮件里的叫法 → 库里可能的叫法）。双向包含匹配已能处理大部分，
# 这里仅补少量纯别名作为兜底。
COMPANY_ALIASES = {
    "字节": "字节跳动", "阿里": "阿里巴巴", "腾讯": "腾讯",
    "京东": "京东", "美团": "美团", "小米": "小米", "百度": "百度",
    "拼多多": "拼多多", "华为": "华为", "网易": "网易", "携程": "携程",
    "哔哩哔哩": "哔哩哔哩", "b站": "哔哩哔哩", "蔚来": "蔚来",
    "理想": "理想汽车", "小鹏": "小鹏汽车", "烽火": "烽火通信",
}

# 状态词前回溯窗口大小（字符）：只在该范围内找公司，避免盘点邮件串到隔壁公司
_COMPANY_LOOKBACK = 40

# ═══════════════════════════════════════════════════════════════
# 聊天一句话状态更新（"启云方进笔试了"/"小米挂了"/"字节oc了"）
# ═══════════════════════════════════════════════════════════════
# 口语化状态关键词（按漏斗优先级排序，高优先级先匹配）
CHAT_STATUS_KW: list[tuple[str, list[str]]] = [
    ("OC",   ["拿到offer", "拿offer了", "拿到offer了", "发了offer", "发offer了",
              "给offer了", "oc了", "录用了", "被录用了", "录用通知", "入职通知"]),
    ("面试", ["进面试", "面试了", "约面试", "一面", "二面", "三面", "终面",
              "群面", "技术面", "hr面", "业务面", "面试通知", "面试邀请"]),
    ("笔试", ["进笔试", "笔试了", "做了笔试", "笔试通知", "在线笔试", "在线测评",
              "测评链接", "机考"]),
    ("已投", ["投了", "投递了", "网申了", "提交了", "投简历了", "已投递", "已投"]),
    ("挂",   ["挂了", "凉了", "被拒了", "拒了", "没戏了", "没过了", "没通过",
              "被淘汰了", "婉拒了", "没过简历", "简历没过"]),
]

# 聊天场景下的连接词/助词，帮助定位状态词
_CHAT_STATUS_SUFFIXES = ("了", "啦", "咯", "呢", "吧")


@dataclass
class DeliveryEvent:
    company: str          # 库内匹配到的公司名（已归一化）
    raw_company: str      # 邮件里识别到的原始公司片段
    status: str           # 已投/笔试/面试/OC/挂
    score: float = 1.0    # 匹配置信度（company 越精确越高）


def is_delivery_email(text: str) -> bool:
    """启发式判断一段文本是不是投递状态邮件。"""
    if not text:
        return False
    t = text.lower()
    if any(k in t for k in DELIVERY_STRONG):
        return True
    if any(p in t for p in PLATFORM_MARKERS) and any(
        w in t for w in ["笔试", "面试", "投递", "录用", "测评", "未通过", "落选"]
    ):
        return True
    return False


def _detect_status(text: str):
    """返回 (status, pos) 列表，按出现位置排序；重叠位置只取最高优先级（先出现者）。"""
    found = []
    low = text.lower()
    for status, kws in STATUS_PATTERNS:
        for kw in kws:
            idx = low.find(kw.lower())
            while idx != -1:
                found.append((status, idx, len(kw)))
                idx = low.find(kw.lower(), idx + 1)
    if not found:
        return []
    found.sort(key=lambda x: x[1])
    result = []
    last_end = -999
    for status, idx, ln in found:
        if idx >= last_end:
            result.append((status, idx))
            last_end = idx + ln
    return result


def _build_company_pool(db_path: str) -> list[str]:
    """从库里取去重公司名；取不到则返回空（退化为只用别名匹配）。"""
    try:
        with contextlib.closing(sqlite3.connect(db_path)) as c:
            rows = c.execute(
                "SELECT DISTINCT company FROM jobs WHERE company IS NOT NULL AND company != ''"
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def canon_company(s: str) -> str:
    """强归一键：全角转半角、去空白/连接符、去括号组、剥法律与集团后缀。

    专门解决「XX有限公司」vs「XX有限责任公司」双向子串都断开的漏配 ——
    两个口径强归一后相等即视为同一家公司。只用于比较，不改变展示名。
    """
    if not s:
        return ""
    t = s.strip().lower()
    # 全角 (FF01-FF5E) → 半角
    t = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in t)
    t = re.sub(r"\([^)]*\)|（[^）]*）", "", t)     # 连内容去掉括号组：(集团)/(中国)
    t = re.sub(r"[\s·・\-—_\[\]【】]", "", t)
    # 循环剥尾缀（「XX股份有限公司」「XX集团有限责任公司」等叠后缀）
    changed = True
    while changed and t:
        changed = False
        for suf in ("股份有限公司", "有限责任公司", "有限公司", "控股集团",
                    "集团公司", "集团", "公司"):
            if t.endswith(suf) and len(t) > len(suf):
                t = t[: -len(suf)]
                changed = True
                break
    return t


def match_company(raw: str, pool: list[str]) -> tuple[str, float]:
    """在库公司池里找最像 raw 的。返回 (库内公司名, 置信度)。"""
    if not raw:
        return "", 0.0
    r = raw.strip()
    # 1) 优先在库公司池里双向包含匹配（最可靠）
    # 连续子串匹配即强信号：2字片段给0.5基线，越长越高，全等1.0
    best, best_score = "", 0.0
    for cand in pool:
        if not cand:
            continue
        if r == cand:
            return cand, 1.0
        if r in cand or cand in r:
            short, long_ = (r, cand) if len(r) <= len(cand) else (cand, r)
            ratio = len(short) / len(long_)
            # 子串匹配最低0.5（2字连续子串已足够确认），线性插值到1.0
            score = 0.5 + 0.5 * ratio
            if score > best_score:
                best, best_score = cand, score
    if best:
        return best, round(best_score, 2)
    # 1.5) 强归一兜底：剥掉法律后缀/全角差异后相等的，视为同一家公司
    #      （「XX有限公司」↔「XX有限责任公司」双向子串都不连续，走到这里才接得住）
    rc = canon_company(r)
    if rc:
        for cand in pool:
            if cand and canon_company(cand) == rc:
                return cand, 0.95
    # 2) 退回别名匹配
    for alias, canon in COMPANY_ALIASES.items():
        if r == alias or r in canon or canon in r:
            return canon, 0.8
    return "", 0.0


# ═══════════════════════════════════════════════════════════════
# 浏览器扩展：抓取名 ↔ 库内公司 的归一化 + 调和（抓取与匹配共用同一口径）
# ═══════════════════════════════════════════════════════════════

# 门户/平台类噪音后缀（"联影网申门户" → "联影"）
_PORTAL_SUFFIX = [
    "网申门户", "网申系统", "网申平台", "招聘门户", "投递门户", "门户系统", "门户",
    "校招官网", "校招网", "招聘官网", "招聘网", "投递系统", "个人中心", "申请中心",
    "我的投递", "投递记录", "申请记录", "网申记录", "招聘首页", "校园招聘",
]
# 明显非公司的词（命中则丢弃，不当作公司名）
_NON_COMPANY_WORDS = {
    "我的", "个人", "申请", "投递", "简历", "登录", "注册", "首页", "招聘", "职位",
    "搜索", "详情", "确认", "成功", "完成", "提交", "欢迎", "中心", "后台", "管理",
    "系统", "通知", "消息", "设置", "帮助", "关于", "协议", "隐私", "用户", "会员",
    "账户", "订单", "购物车", "收藏", "历史", "足迹", "记录",
}
# 域名 → 公司名（大厂在扩展端已覆盖，这里补中厂/校招官网）
_DOMAIN_COMPANY_SERVER = {
    "hellogroup": "挚文集团", "immomo": "挚文集团",
    "united-imaging": "联影医疗", "uii": "联影医疗",
    "duoyi": "多益网络", "xy": "多益网络",
    "fun.tv": "fun.tv",
}


def normalize_company(raw: str) -> str:
    """把抓取到的公司名归一：去【】季节前缀、去门户/平台后缀、去前导噪音。"""
    if not raw:
        return ""
    s = raw.strip()
    s = re.sub(r"^【[^】]{0,20}】", "", s)            # 【2026校招】
    s = re.sub(r"^(?:秋招|春招|校招|社招|实习)[-_－\s]*", "", s)
    for suf in _PORTAL_SUFFIX:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    s = re.sub(r"^(?:我的|个人)[-_－\s]*", "", s)
    s = s.strip(" ·-_－")
    return s


def is_junk_company(s: str) -> bool:
    if not s:
        return True
    return s in _NON_COMPANY_WORDS or len(s) < 2


def infer_company_from_url(url: str) -> str:
    """从网申页 URL 推断公司（兜底）。"""
    if not url:
        return ""
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    except Exception:
        return ""
    for d, name in _DOMAIN_COMPANY_SERVER.items():
        if d in host:
            return name
    return ""


def reconcile_company(raw: str, pool: list[str], url: str = "") -> tuple[str, float, str]:
    """抓取名 → 库内公司 的调和。返回 (库内/归一公司名, 置信度, 来源)。

    来源:
      pool   —— 命中库内已有公司（用于回写漏斗，即"同步"）
      alias  —— 命中别名表
      domain —— 域名推断
      new    —— 归一后是真实新公司名（建新行）
      none   —— 仍是垃圾，无法识别
    """
    norm = normalize_company(raw)
    if norm:
        m, score = match_company(norm, pool)
        if m and score >= 0.5:
            return m, score, "pool"
    if norm:
        for alias, canon in COMPANY_ALIASES.items():
            if norm == alias or norm in canon or canon in norm:
                return canon, 0.8, "alias"
    dom = infer_company_from_url(url)
    if dom:
        m, score = match_company(dom, pool)
        if m and score >= 0.5:
            return m, score, "pool"
        return dom, 0.6, "domain"
    if norm and not is_junk_company(norm):
        return norm, 0.5, "new"
    return "", 0.0, "none"


def find_extension_row(c, companies: list[str]):
    """在浏览器扩展来源的行里按归一公司名查重，防止重复新建投递记录。

    companies 传多个口径（原始归一名 + reconcile 规范名），只要归一后
    与任一口径相同即视为同一投递。返回 sqlite3.Row 或 None。
    """
    norms = {normalize_company(x) for x in companies if x}
    norms.discard("")
    rows = c.execute(
        "SELECT raw_hash,company,title,status FROM jobs WHERE source LIKE '浏览器扩展%'"
    ).fetchall()
    if not rows:
        return None
    # 1) 归一名精确相等
    for r in rows:
        if normalize_company(r["company"]) in norms:
            return r
    # 2) 模糊兜底：有限公司/股份等后缀差异
    ext_pool = [r["company"] for r in rows]
    for comp in companies:
        if not comp:
            continue
        m, s = match_company(comp, ext_pool)
        if m and s >= 0.6:
            for r in rows:
                if r["company"] == m:
                    return r
    return None


def _extract_company_before(before: str, pool: list[str]) -> str:
    """在状态词『之前』的文本里找公司：取文本中最后出现的（最靠近状态词）。

    按「文本位置」比较而非候选列表迭代序 —— 盘点邮件（美团…字节跳动…[状态词]）
    里迭代序会按 pool 字典序误选，写错公司行。
    """
    candidates = list(pool) + list(COMPANY_ALIASES.keys()) + list(COMPANY_ALIASES.values())
    best, best_pos = "", -1
    for cand in candidates:
        if not cand:
            continue
        p = before.rfind(cand)
        if p < 0:
            continue
        # 位置更靠后 = 更靠近状态词；同位置取更长名（「字节跳动」优先于「字节」）
        if p > best_pos or (p == best_pos and len(cand) > len(best)):
            best, best_pos = cand, p
    if best:
        return best
    m = re.search(
        r"(?:投递的|来自|您向|向)\s*([一-龥A-Za-z·]{2,20}?)\s*(?:的|公司)?\s*(?:岗位|职位|简历|校招|秋招|网申)",
        before,
    )
    if m:
        return m.group(1).strip("· ")
    m = re.search(
        r"([一-龥A-Za-z·]{2,16}?)(?:公司|集团|科技|网络|软件|信息技术)?\s*(?:笔试|面试|录用|邀您|邀请)",
        before,
    )
    if m:
        return m.group(1).strip("· ")
    return ""


_DB_HINT = None


def _db_path_hint() -> str:
    return _DB_HINT or str(Path(__file__).resolve().parent.parent / "hermes.db")


def set_db_path(db_path: str):
    global _DB_HINT
    _DB_HINT = db_path


def _advance_ok(current: str, new: str) -> bool:
    """漏斗只前进（挂可覆盖任意前置态）。

    「挂」是终态：迟到的投递确认邮件（挂=-1 之后到达的 已投=2）不得把状态复活。
    真标错了需要纠正时走看板手动改（手动路径不经本守卫）。
    """
    if new == "挂":
        return True
    if current == "挂":
        return False
    cur = STATUS_RANK.get(current, 0)
    nv = STATUS_RANK.get(new, 0)
    return nv > cur


def parse_delivery_email(text: str) -> list[DeliveryEvent]:
    """从一封（可能含多条的）邮件文本抽取所有 (公司, 状态) 事件。"""
    if not text or not is_delivery_email(text):
        return []
    pool = _build_company_pool(_db_path_hint())
    status_hits = _detect_status(text)
    events = []
    for status, pos in status_hits:
        before = text[max(0, pos - _COMPANY_LOOKBACK): pos]
        raw = _extract_company_before(before, pool)
        if raw:
            company, conf = match_company(raw, pool)
            if company:
                events.append(DeliveryEvent(company=company, raw_company=raw, status=status, score=conf))
    # 去重：同一公司同状态只留一条（置信度最高）
    seen = {}
    for e in events:
        key = (e.company, e.status)
        if key not in seen or e.score > seen[key].score:
            seen[key] = e
    return list(seen.values())


def apply_events(events: list[DeliveryEvent], db_path: str) -> dict:
    """把解析出的事件回写到 jobs 库。返回汇总（同公司多事件折叠为最终状态）。"""
    applied_map: dict[str, str] = {}     # company -> 最终状态
    applied_order: list[str] = []
    skipped, not_found = [], []
    with contextlib.closing(sqlite3.connect(db_path)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        for e in events:
            rows = c.execute(
                "SELECT raw_hash, status, match_score FROM jobs WHERE company=? ORDER BY match_score DESC",
                (e.company,),
            ).fetchall()
            if not rows:
                not_found.append(e.company)
                continue
            # 一司多岗：优先更新已进投递漏斗的那行（用户实际投的是它），
            # 多行都在漏斗时取推进最深的；都不在漏斗才退回最高分行。
            in_funnel = [r for r in rows if r["status"] in ("已投", "笔试", "面试", "OC")]
            target = max(in_funnel, key=lambda r: STATUS_RANK.get(r["status"], 0)) if in_funnel else rows[0]
            if _advance_ok(target["status"], e.status):
                c.execute("UPDATE jobs SET status=? WHERE raw_hash=?", (e.status, target["raw_hash"]))
                if e.company not in applied_map:
                    applied_order.append(e.company)
                applied_map[e.company] = e.status
            else:
                skipped.append((e.company, target["status"], e.status))
        c.commit()
    return {
        "applied": [(co, applied_map[co]) for co in applied_order],
        "skipped": skipped,
        "not_found": not_found,
    }


def sync_email_text(text: str, db_path: str) -> str:
    """高层入口：解析 + 回写 + 人类可读汇总。供聊天/工具调用。"""
    set_db_path(db_path)
    events = parse_delivery_email(text)
    if not events:
        return ("📭 没在这段内容里识别出投递状态（需要含「投递成功/笔试/面试/录用/未通过」等字样，"
                "且能识别公司名）。可把邮件正文直接粘贴过来，或在 QQ 邮箱设置自动转发。")
    res = apply_events(events, db_path)
    lines = ["📨 已解析投递邮件，更新如下："]
    for company, status in res["applied"]:
        lines.append(f"  ✅ {company} → {status}")
    for company, cur, want in res["skipped"]:
        lines.append(f"  ⏭️ {company} 已是 {cur}，跳过 {want}（漏斗不回退）")
    for company in res["not_found"]:
        lines.append(f"  ❓ 库里没有「{company}」的岗位，未更新（先「扫岗位」补充来源？）")
    lines.append(f"\n共更新 {len(res['applied'])} 条，已在 /jobs 投递看板可见。")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 聊天一句话状态更新
# "启云方进笔试了" / "小米面试了" / "字节oc了" / "烽火挂了"
# ═══════════════════════════════════════════════════════════════

def _find_chat_status(text: str) -> list[tuple[str, int]]:
    """从口语化文本中找 (状态, 关键词位置) 列表。"""
    low = text.lower()
    hits = []
    for status, kws in CHAT_STATUS_KW:
        for kw in kws:
            idx = low.find(kw.lower())
            if idx >= 0:
                hits.append((status, idx, len(kw)))
    if not hits:
        return []
    # 按位置排序，去重叠（同位置只取最高优先级）
    hits.sort(key=lambda x: x[1])
    result = []
    last_end = -999
    for status, idx, ln in hits:
        if idx >= last_end:
            result.append((status, idx))
            last_end = idx + ln
    return result


def _extract_company_from_chat(text: str, pool: list[str]) -> str:
    """从口语化聊天中提取公司名。三步：库全名命中→正则抽片段→别名兜底。"""
    # 1) 库公司全名 / 别名 in 用户文本（正向匹配）
    candidates = list(pool) + list(COMPANY_ALIASES.keys()) + list(COMPANY_ALIASES.values())
    best, best_score = "", 0.0
    for cand in candidates:
        if not cand:
            continue
        if cand in text:
            score = len(cand)
            if score > best_score:
                best, best_score = cand, score
    if best:
        matched, _ = match_company(best, pool)
        return matched
    # 2) 正则抽取：状态词附近 2-16 个中文字符片段，尝试双向包含匹配库
    # 去掉状态词后的部分，只看"前半句"（公司名通常在状态词前面）
    status_kws_all = []
    for _, kws in CHAT_STATUS_KW:
        status_kws_all.extend(kws)
    earliest_pos = len(text)
    low = text.lower()
    for kw in status_kws_all:
        idx = low.find(kw.lower())
        if 0 <= idx < earliest_pos:
            earliest_pos = idx
    before_text = text[:earliest_pos] if earliest_pos < len(text) else text
    # 从 before_text 提取所有可能的公司名片段
    fragments = re.findall(r"([一-龥A-Za-z·]{2,16}(?:公司|集团|科技|网络|软件|信息技术|有限)?)", before_text)
    if not fragments:
        fragments = re.findall(r"([一-龥A-Za-z·]{2,16})", before_text)
    for frag in fragments:
        frag = frag.strip()
        if len(frag) < 2:
            continue
        matched, conf = match_company(frag, pool)
        if matched and conf >= 0.3:
            return matched
        # 截断尾字再试（"启云方进" → "启云方" → 命中"武汉启云方科技有限公司"）
        for trim in range(1, min(4, len(frag))):
            sub = frag[:-trim]
            if len(sub) < 2:
                break
            matched, conf = match_company(sub, pool)
            if matched and conf >= 0.3:
                return matched
    return ""


def parse_chat_status_update(text: str, db_path: str) -> tuple[str, list[DeliveryEvent]] | None:
    """识别口语化状态更新，返回 (库内公司名, [events]) 或 None。

    用法示例：
      "启云方进笔试了" → ("武汉启云方科技有限公司", [DeliveryEvent(status="笔试")])
      "小米挂了"       → ("小米", [DeliveryEvent(status="挂")])
    """
    if not text or len(text) > 100:
        return None  # 太长不像是一句话更新
    set_db_path(db_path)
    status_hits = _find_chat_status(text)
    if not status_hits:
        return None
    pool = _build_company_pool(db_path)
    company = _extract_company_from_chat(text, pool)
    if not company:
        return None
    # 取最高优先级的状态（位置最前的）
    status = status_hits[0][0]
    events = [DeliveryEvent(company=company, raw_company=company, status=status, score=0.9)]
    return company, events


def sync_chat_status(text: str, db_path: str) -> str | None:
    """高层入口：识别一句话状态更新 → 回写 → 返回汇总文本。不识别返回 None。"""
    result = parse_chat_status_update(text, db_path)
    if result is None:
        return None
    company, events = result
    res = apply_events(events, db_path)
    if res["applied"]:
        return f"✅ {company} → {res['applied'][0][1]}（已更新投递看板）"
    if res["skipped"]:
        _, cur, want = res["skipped"][0]
        return f"⏭️ {company} 已是「{cur}」，跳过「{want}」（漏斗不回退）"
    if res["not_found"]:
        return f"❓ 库里没有「{company}」的岗位"
    return None


# ═══════════════════════════════════════════════════════════════
# 浏览器扩展读「我的投递」列表页：批量 (公司, 状态) 解析 + 回写
# 扩展把页面上每条投递记录行的纯文本发来，这里逐行抽公司+状态。
# ═══════════════════════════════════════════════════════════════

def _extract_company_in_row(row: str, pool: list[str]) -> str:
    """从「我的投递」页的一行文本里抽公司名。复用 match_company（连续子串基线0.5）。"""
    # 1) 库全名/别名正向命中（最长优先）
    candidates = list(pool) + list(COMPANY_ALIASES.keys()) + list(COMPANY_ALIASES.values())
    best, best_len = "", 0
    for cand in candidates:
        if cand and cand in row and len(cand) > best_len:
            best, best_len = cand, len(cand)
    if best:
        m, _ = match_company(best, pool)
        return m
    # 2) 正则抽片段 → 优先库内匹配；库外则保留原始片段用于新建真实记录
    frags = re.findall(r"([一-龥A-Za-z·]{2,16}(?:公司|集团|科技|网络|软件|信息技术|有限)?)", row)
    fallback = ""
    for f in frags:
        f = f.strip()
        if len(f) < 2:
            continue
        m, conf = match_company(f, pool)
        if m and conf >= 0.3:
            return m
        for trim in range(1, min(4, len(f))):
            sub = f[:-trim]
            if len(sub) < 2:
                break
            m2, conf2 = match_company(sub, pool)
            if m2 and conf2 >= 0.3:
                return m2
        # 库内无匹配：记第一个像公司名的片段（含公司/科技等后缀 或 ≥3字）作兜底，用于新建
        if not fallback and (len(f) >= 3 or any(s in f for s in ("公司", "科技", "集团", "网络", "软件", "信息技术", "有限"))):
            fallback = normalize_company(f)
    return fallback


def _extract_title_in_row(row: str, company: str) -> str:
    """从一行抽岗位名（可选）。失败返回空，不影响主流程。"""
    m = re.search(r"(?:职位|岗位|应聘|申请)[：: ]*([一-龥A-Za-z0-9/·()（）]{2,30})", row)
    if m:
        return m.group(1).strip()
    rest = row.replace(company, "", 1)
    m2 = re.search(
        r"([一-龥A-Za-z0-9/·]{2,30}职[位业]|[一-龥]{2,20}(?:工程师|开发|运营|产品|测试|算法|分析师|专员|助理|管培))",
        rest,
    )
    if m2:
        return m2.group(1).strip()
    return ""


def _detect_status_in_row(row: str):
    """优先平台用语(STATUS_PATTERNS)，其次口语(CHAT_STATUS_KW)，返回最高优先级状态或 None。"""
    sh = _detect_status(row)
    if sh:
        return sh[0][0]
    ch = _find_chat_status(row)
    if ch:
        return ch[0][0]
    return None


def parse_delivery_list(rows: list[str], db_path: str, url: str = "") -> dict:
    """解析扩展从「我的投递」列表页抓来的一行行文本 → 批量回写看板。

    库内已有该岗位：按漏斗更新状态；库内没有：新建真实投递记录
    （source='浏览器扩展-我的投递'，看板「只看真实投递」可见）。
    返回人类可读汇总。
    """
    set_db_path(db_path)
    pool = _build_company_pool(db_path)
    # 公司自有招聘门户（多益/小米等）：URL 域名推断出的公司直接采信。
    # 原因：这类页面「我的应聘」行内不含公司名，行内正则抽到的只能是噪音
    # （如"等待面试安排"），还容易误匹配到库内相似名（如"软件工程师"→"原创软件"）。
    url_company = ""
    if url:
        dom = infer_company_from_url(url)
        if dom:
            m, score = match_company(dom, pool)
            url_company = m if (m and score >= 0.5) else dom
    found: list[tuple[str, str, str]] = []  # (company, title, status)
    for row in rows:
        row = (row or "").strip()
        if not row:
            continue
        status = _detect_status_in_row(row)
        if not status:
            # 兜底：含"投递/提交"但无明确阶段词 → 视为已投
            if "投递" in row or "提交" in row:
                status = "已投"
            else:
                continue
        if url_company:
            company = url_company
        else:
            company = _extract_company_in_row(row, pool)
        if not company:
            continue
        title = _extract_title_in_row(row, company)
        found.append((company, title, status))
    return _apply_delivery_list(found, db_path)


def _apply_delivery_list(found: list[tuple[str, str, str]], db_path: str) -> dict:
    created, updated, skipped = [], [], []
    pool = _build_company_pool(db_path)  # 循环外构建一次，避免 N+1 全表扫描
    with contextlib.closing(sqlite3.connect(db_path)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        for company, title, status in found:
            matched, conf = match_company(company, pool)
            target = None
            if matched and conf >= 0.3:
                if title:
                    rows = c.execute(
                        "SELECT raw_hash,status FROM jobs WHERE company=? AND title LIKE ?",
                        (matched, "%" + title + "%"),
                    ).fetchall()
                    if rows:
                        target = rows[0]
                if target is None:
                    rows = c.execute(
                        "SELECT raw_hash,status FROM jobs WHERE company=? ORDER BY match_score DESC",
                        (matched,),
                    ).fetchall()
                    if rows:
                        target = rows[0]
            if target is not None:
                if _advance_ok(target["status"], status):
                    c.execute("UPDATE jobs SET status=? WHERE raw_hash=?", (status, target["raw_hash"]))
                    updated.append((matched, status))
                else:
                    skipped.append((matched, target["status"], status))
            else:
                # 未命中库内公司 → 先在扩展自有投递里查重（归一公司口径），避免重复新建
                ext = find_extension_row(c, [company])
                if ext is not None:
                    if _advance_ok(ext["status"], status):
                        c.execute("UPDATE jobs SET status=? WHERE raw_hash=?", (status, ext["raw_hash"]))
                        updated.append((ext["company"], status))
                    else:
                        skipped.append((ext["company"], ext["status"], status))
                    continue
                raw_hash = hashlib.md5(f"{company}|{title}".encode("utf-8")).hexdigest()
                c.execute(
                    """INSERT OR IGNORE INTO jobs
                       (raw_hash,company,title,city,education,batch,deadline,url,link_type,source,
                        nature,industry,major,school_level,salary,first_seen,match_score,match_reason,status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (raw_hash, company, title or "投递岗位", "", "", "", "", "", "源详情页",
                     "浏览器扩展-我的投递", "", "", "", "", "", datetime.now().strftime("%Y-%m-%d"),
                     0, "浏览器扩展读我的投递页", status),
                )
                created.append((raw_hash, company, title, status))
        c.commit()
    return {
        "ok": True,
        "created": [{"raw_hash": h, "company": co, "title": ti, "status": st} for h, co, ti, st in created],
        "updated": [{"company": co, "status": st} for co, st in updated],
        "skipped": [{"company": co, "cur": cur, "want": want} for co, cur, want in skipped],
        "total": len(found),
    }
