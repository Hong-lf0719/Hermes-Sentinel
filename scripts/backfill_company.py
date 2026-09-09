# -*- coding: utf-8 -*-
"""回填脏公司名：company 为空 / '会员可见' 的岗位，从标题头部抽公司名。

背景：2026-09-02 体检发现库内 161 条 company 为空（华科 152 + 武大 9）、
20 条 company='会员可见'（offer多多付费墙字段）。空公司名会导致
① 去重按 (company,title) 碰撞、互相顶掉；② 看板卡片无公司、投递记录错挂。

新扫描已由 hust_jobs._company_from_title 处理，本次只回填存量。
抽不出公司名的保留原值（不臆造），仅打标 company_unknown 便于后续人工补。
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "hermes.db"

# 标题中「公司名」与后半段招聘描述的分界。
# 注意「届」必须与年份连用，否则「2027届杭州飞仕得…」会被切成 head="2027"。
_CUT_RE = re.compile(
    r"(20\d{2}\s*[年届]|校园招聘|校招|实习生招聘|招聘简章|招聘公告|招聘启事|"
    r"公开招聘|宣讲会|空中宣讲|补录|秋招|春招|提前批|招聘)"
)
# 整条标题本身就是公司名（无分界词时兜底）
_SUFFIX_RE = re.compile(
    r"(有限公司|股份有限公司|有限责任公司|集团公司|集团|研究院|研究所|"
    r"银行|大学|学院|医院|科技|技术)$"
)
# 头部剥离：届别/年份前缀
_HEAD_PREFIX_RE = re.compile(r"^20\d{2}\s*[届年]\s*")
_NON_COMPANY_HEADS = (
    "关于", "通知", "公告", "转发", "举办", "开展", "组织", "邀请", "提醒",
    "温馨提示", "重要", "快讯", "速递", "汇总", "速览", "一览", "信息",
    "秋季", "春季", "全国", "博士", "硕士",
)
# 会员可见 / 付费墙占位值
_PLACEHOLDERS = {"会员可见", "会员", "可见", "-", "—", "暂无", "未知", "无"}


def company_from_title(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return ""
    # 先剥「2027届」这类前缀，否则切分点落在位置 0，head 为空
    t = _HEAD_PREFIX_RE.sub("", t).strip(" -—·:：()（）[]【】|/、,，")
    m = _CUT_RE.search(t)
    if m:
        head = _HEAD_PREFIX_RE.sub("", t[: m.start()]).strip(" -—·:：()（）[]【】|/、,，")
        # 公司名一般不超过 30 字，更长的基本是「双选会/专场」这类活动标题
        if 2 <= len(head) <= 30 and not head.startswith("http") \
                and not any(head.startswith(h) for h in _NON_COMPANY_HEADS) \
                and head not in _PLACEHOLDERS:
            return head
    # 兜底：整条标题就是公司名（如「汇丰软件开发（广东）有限公司」）
    if 4 <= len(t) <= 40 and _SUFFIX_RE.search(t) and t not in _PLACEHOLDERS:
        return t
    return ""


# 公司名尾部残留的届别/年份（「宁波银行总行金融科技部2027」「宁德新能源(ATL)2027全球」）
_TAIL_RE = re.compile(r"[\s(（]*20\d{2}\s*[届年]?[\s)）]*$")


def clean_tails(conn, apply: bool) -> int:
    """清掉已有公司名尾部的年份/届别残留。"""
    rows = conn.execute(
        "SELECT id, company FROM jobs WHERE company IS NOT NULL AND TRIM(company)<>''"
    ).fetchall()
    n = 0
    for r in rows:
        c = (r["company"] or "").strip()
        c2 = _TAIL_RE.sub("", c).strip(" -—·:：")
        # 别把「中芯国际2025」这种整体年份名削成空
        if c2 and c2 != c and len(c2) >= 2:
            n += 1
            if apply:
                conn.execute("UPDATE jobs SET company=? WHERE id=?", (c2, r["id"]))
    if apply and n:
        conn.commit()
    return n


def _is_wall(title: str) -> bool:
    """标题本身是付费墙占位（无公司、无岗位信息）。"""
    t = (title or "").strip()
    return (not t) or t in _PLACEHOLDERS or ("登录后可见" in t) or t == "登录"


def main(apply: bool = False) -> int:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, company, title, source FROM jobs "
        "WHERE company IS NULL OR TRIM(company)='' OR TRIM(company) IN ('会员可见','会员','-')"
    ).fetchall()
    print(f"待处理 {len(rows)} 条")

    done, skipped, hidden, cleaned = 0, [], 0, 0
    for r in rows:
        name = company_from_title(r["title"] or "")
        if name:
            done += 1
            if apply:
                conn.execute("UPDATE jobs SET company=? WHERE id=?", (name, r["id"]))
            continue
        # 抽不出公司名 + 标题本身是付费墙占位（offer多多「登录后可见」）→ 隐藏，
        # 这类行没有任何可用信息，留在看板只是噪音（隐藏非删除，可恢复）
        if _is_wall(r["title"] or ""):
            hidden += 1
            if apply:
                conn.execute("UPDATE jobs SET user_hidden=1 WHERE id=?", (r["id"],))
            continue
        # 公司名是付费墙占位值（「会员可见」）→ 置空，宁可空着也不把脏值当公司名
        if (r["company"] or "").strip() in _PLACEHOLDERS:
            cleaned += 1
            if apply:
                conn.execute("UPDATE jobs SET company='' WHERE id=?", (r["id"],))
            continue
        skipped.append((r["id"], (r["company"] or ""), (r["title"] or "")[:40]))
    if apply:
        conn.commit()
    conn.close()

    print(f"可回填 {done} 条，占位公司名清空 {cleaned} 条，付费墙隐藏 {hidden} 条，"
          f"其余保留 {len(skipped)} 条")
    for i, (cid, comp, title) in enumerate(skipped[:15]):
        print(f"   跳过 id={cid} company={comp!r} title={title}")
    if len(skipped) > 15:
        print(f"   … 其余 {len(skipped)-15} 条省略")
    if not apply:
        print("\n（预览模式，未写库。加 --apply 执行）")
    return 0


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
