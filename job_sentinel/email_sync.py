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
    ("面试", ["面试邀请", "面试通知", "邀您面试", "面试邀约", "一面通知", "二面通知",
              "终面通知", "视频面试", "现场面试", "面试安排", "群面通知"]),
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
    # 2) 退回别名匹配
    for alias, canon in COMPANY_ALIASES.items():
        if r == alias or r in canon or canon in r:
            return canon, 0.8
    return "", 0.0


def _extract_company_before(before: str, pool: list[str]) -> str:
    """在状态词『之前』的文本里找公司：取最后出现的（最靠近状态词，避免串到隔壁公司）。"""
    candidates = list(pool) + list(COMPANY_ALIASES.keys()) + list(COMPANY_ALIASES.values())
    last_hit = ""
    for cand in candidates:
        if cand and cand in before:
            last_hit = cand  # 后续命中覆盖，最终是最后一个（最靠近状态词）
    if last_hit:
        return last_hit
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
    """漏斗只前进（挂可覆盖任意前置态）。"""
    if new == "挂":
        return True
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
            target = rows[0]
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
            fallback = f
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


def parse_delivery_list(rows: list[str], db_path: str) -> dict:
    """解析扩展从「我的投递」列表页抓来的一行行文本 → 批量回写看板。

    库内已有该岗位：按漏斗更新状态；库内没有：新建真实投递记录
    （source='浏览器扩展-我的投递'，看板「只看真实投递」可见）。
    返回人类可读汇总。
    """
    set_db_path(db_path)
    pool = _build_company_pool(db_path)
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
