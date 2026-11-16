"""Hermes 匹配层（2026-11-16 从 server.py 拆出）——匹配中心核心接口。

五维公司匹配（岗位 40 / 学历友好 25 / 福利 15 / 偏好 10 / 历史回应 10）+ 60s TTL 缓存。
打分实现在 job_sentinel/matcher.py；本模块负责聚合、缓存与 HTTP 路由。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path as _P

from fastapi import APIRouter, Depends

from hermes_common import get_db, require_auth

router = APIRouter()

# ── 匹配结果缓存：5732 行 × 2034 公司的全量聚合不该每次请求都重算 ──
# 60s TTL + 主动失效（状态变更/扫描/重算/情报更新时由调用方 invalidate）。
# key 绑定哨兵画像 updated_at：改偏好立即算新 key，不会返回旧画像的分数。
_MATCH_TTL_S = 60.0
_MATCH_CACHE: dict = {"key": None, "ts": 0.0, "data": None}


def invalidate_match_cache() -> None:
    """岗位/情报/画像变更后调用，让下一次 /api/match/companies 强制重算。"""
    _MATCH_CACHE["key"] = None


@router.get("/api/match/companies")
async def api_match_companies():
    """匹配中心核心接口：按公司聚合岗位 → 五维匹配度打分 → 推荐排序。

    五维：岗位匹配 40（该公司最高/平均 match_score）· 学历友好 25（岗位学历文本 +
    公司情报实搜门槛 + 对双非/民办的实际态度）· 福利 15（公司福利 ∩ 福利偏好）·
    偏好 10（性质/行业/加班接受度×口碑）· 历史回应 10（你对该性质公司的实测
    回应率，样本≥3 才计权）。
    """
    from job_sentinel.matcher import company_match
    from job_sentinel.profile import load_profile

    prof_path = _P(__file__).parent / "job_sentinel" / "profile.json"
    profile = load_profile(prof_path)

    key = f"v1|{profile.get('updated_at', '')}|{datetime.now():%Y-%m-%d}"
    now = time.time()
    if (_MATCH_CACHE["key"] == key and _MATCH_CACHE["data"] is not None
            and now - _MATCH_CACHE["ts"] < _MATCH_TTL_S):
        return _MATCH_CACHE["data"]

    # 公司情报 map（含 7 天内有效性标注）
    intel_map: dict = {}
    try:
        with get_db() as db:
            for r in db.execute("SELECT company, intel_json, searched_at FROM company_intel").fetchall():
                try:
                    intel_map[r["company"]] = {
                        "intel": json.loads(r["intel_json"]),
                        "stale": (datetime.now() - datetime.strptime(r["searched_at"][:19], "%Y-%m-%d %H:%M:%S")).days >= 7,
                    }
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    # 历史回应率（按企业性质；样本 ≥3 的才传给打分器）
    resp_by_type: dict = {}
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT enterprise_type AS g, status, COUNT(*) AS n FROM jobs "
                "WHERE status IN ('已投','笔试','面试','OC','挂') "
                "AND TRIM(COALESCE(enterprise_type,'')) != '' "
                "GROUP BY enterprise_type, status"
            ).fetchall()
        agg: dict = {}
        for r in rows:
            g = agg.setdefault(r["g"], {"applied": 0, "responded": 0})
            g["applied"] += r["n"]
            if r["status"] in ("笔试", "面试", "OC", "挂"):
                g["responded"] += r["n"]
        resp_by_type = {g: {"applied": v["applied"],
                            "response_rate": round(v["responded"] / v["applied"], 2) if v["applied"] else 0}
                        for g, v in agg.items()}
    except Exception:  # noqa: BLE001
        pass

    # 公司聚合（含隐藏岗但排除占位公司——占位岗已在入库时过滤）
    companies: dict = {}
    try:
        with get_db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE user_hidden=0").fetchall()
        for r in rows:
            d = dict(r)
            companies.setdefault(d.get("company") or "未知公司", []).append(d)
    except Exception:  # noqa: BLE001
        pass

    out = []
    for company, jrows in companies.items():
        intel_entry = intel_map.get(company)
        intel = (intel_entry or {}).get("intel")
        resp = resp_by_type.get((jrows[0].get("enterprise_type") or "").strip())
        m = company_match(jrows, profile, intel, resp)
        best = max(jrows, key=lambda j: j.get("match_score") or 0)
        m["best_job"] = {"title": best.get("title"), "city": best.get("city"),
                         "deadline": best.get("deadline"), "salary": best.get("salary"),
                         "url": best.get("url"), "batch": best.get("batch")}
        m["company"] = company
        m["intel_stale"] = bool(intel_entry and (intel_entry or {}).get("stale"))
        m["has_intel"] = bool(intel_entry)
        iv = (intel_entry or {}).get("intel") or {}
        m["school_barrier"] = iv.get("school_barrier") or ""
        m["school_note"] = iv.get("school_note") or ""
        m["best_url"] = best.get("url") or ""
        out.append(m)
    out.sort(key=lambda m: -m["score"])
    payload = {"companies": out, "count": len(out), "profile": profile}
    _MATCH_CACHE.update({"key": key, "ts": now, "data": payload})
    return payload
