"""Hermes 情报层（2026-11-16 从 server.py 拆出）——公司情报卡与批量补全。

面向"民办二本投大厂容易石沉大海"的真实痛点：投之前先看这家公司
对学历的态度、双非友好度、福利、笔面试难度、口碑与近期稳定性。
六路公开检索 + LLM 结构化汇总，7 天缓存；旧 schema 卡自动重研升级。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from pathlib import Path as _P

from fastapi import APIRouter, Depends, HTTPException

from hermes_common import DB_PATH, _SHUTDOWN, _ai_json, _cached, _now_local, get_db, load_profile, logger, require_auth
from hermes_search import web_search

router = APIRouter()

_COMPANY_INTEL_TTL_DAYS = 7
_INTEL_QUERIES = [
    "{c} 校园招聘 学历要求 985 211 双非",
    "{c} 员工评价 加班 工作强度",
    "{c} 裁员 扩张 经营状况",
    "{c} 面经 面试经验 校招",
    "{c} 福利待遇 包住 补贴 六险一金",
    "{c} 校招 双非 二本 普通本科 能投吗",
]


def _save_company_intel(company: str, intel: dict) -> None:
    from hermes_match import invalidate_match_cache
    with get_db() as db:
        db.execute(
            "INSERT INTO company_intel (company, intel_json, searched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(company) DO UPDATE SET intel_json=excluded.intel_json, "
            "searched_at=excluded.searched_at",
            (company, json.dumps(intel, ensure_ascii=False), _now_local()),
        )
        db.commit()
    invalidate_match_cache()   # 新情报改变五维中的学历/福利维度，立即失效匹配缓存


def _load_company_intel(company: str) -> dict | None:
    with get_db() as db:
        r = db.execute(
            "SELECT intel_json, searched_at FROM company_intel WHERE company = ?", (company,)
        ).fetchone()
    if not r:
        return None
    try:
        return {"intel": json.loads(r["intel_json"]), "searched_at": r["searched_at"]}
    except Exception:  # noqa: BLE001
        return None


def _intel_stale(cached: dict) -> bool:
    sa = (cached.get("searched_at") or "")[:19]
    try:
        from datetime import datetime
        dt = datetime.strptime(sa, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - dt).days >= _COMPANY_INTEL_TTL_DAYS
    except Exception:  # noqa: BLE001
        return True


def _intel_fresh(cached: dict | None) -> bool:
    """情报"新鲜且新 schema"才算可用。

    旧 schema 卡（2026-11-15 之前，只有学历门槛/口碑，没有
    school_tier_friendly/welfare/difficulty）即使 7 天内也自动重研升级，
    保证匹配中心五维打分与情报弹窗拿得到新字段。
    """
    if not cached or _intel_stale(cached):
        return False
    return "school_tier_friendly" in (cached.get("intel") or {})


def _intel_reply_text(company: str, intel: dict) -> str:
    bar = intel.get("school_barrier") or "未知"
    tf = intel.get("school_tier_friendly") or "未知"
    df = intel.get("difficulty") or "未知"
    lines = [f"🏢 {company} · 学历门槛：{bar} · 对双非：{tf} · 笔面试：{df}（置信度 {intel.get('confidence', '低')}）"]
    if intel.get("school_note"):
        lines.append(f"· {intel['school_note']}")
    if intel.get("school_tier_note"):
        lines.append(f"· 院校态度依据：{intel['school_tier_note']}")
    wf = [w for w in (intel.get("welfare") or []) if w]
    if wf:
        lines.append("🎁 福利：" + "、".join(wf[:6]))
    for k, icon in (("employee_voice", "🗣 员工口碑"), ("recent_moves", "📈 近期动态"), ("interview_tips", "🧪 面经要点")):
        vals = intel.get(k) or []
        if vals:
            lines.append(f"{icon}：" + "；".join(vals[:3]))
    if intel.get("w2w_advice"):
        lines.append(f"💡 建议：{intel['w2w_advice']}")
    lines.append("（信息来自公开搜索摘要，供参考非事实结论）")
    return "\n".join(lines)


async def company_research(company: str = "", focus: str = "") -> str:
    """工具入口：多路搜索 + LLM 结构化汇总，生成公司情报卡（7 天缓存）。"""
    company = (company or "").strip()
    if not company:
        return "⚠️ 请告诉我要调研的公司名"
    cached = _load_company_intel(company)
    if _intel_fresh(cached):
        return _intel_reply_text(company, cached["intel"])

    queries = [q.format(c=company) for q in _INTEL_QUERIES]
    gathered = await asyncio.gather(
        *[_cached(f"intel_{company}_{i}", 600, lambda qs=q: web_search(qs, 6))
          for i, q in enumerate(queries)],
        return_exceptions=True,
    )
    texts = []
    for i, r in enumerate(gathered):
        body = r if isinstance(r, str) else f"搜索失败: {r}"
        texts.append(f"【检索{i + 1}】{body}")
    material = "\n".join(texts)[:6000]

    # 用户背景动态注入（哨兵画像 + user_profile KV，不再硬编码）
    from job_sentinel.profile import load_profile as load_sentinel_profile
    prof_path = _P(__file__).parent / "job_sentinel" / "profile.json"
    prof = load_sentinel_profile(prof_path)
    kv = load_profile()
    user_bg = [
        f"院校层次：{prof.get('school_tier', '民办二本/独立学院')}（{kv.get('学校', '')}）",
        f"学历：{prof.get('education', '本科')}（{kv.get('毕业年份', '2027')} 届）",
        f"专业：{kv.get('专业', '')}",
        f"技能栈：{kv.get('技能栈', '')}",
        f"求职方向：{(prof.get('directions') or [])}",
        f"目标城市：{(prof.get('regions') or [])}",
        f"加班接受度：{prof.get('accept_overtime', '中')}（用户最反感口碑里的高强度加班时，请在建议中体现）",
    ]

    prompt = f"""你是求职情报分析师。用户背景：
{chr(10).join(user_bg)}
调研公司：{company}{('，重点关注：' + focus) if focus else ''}

以下是六路公开搜索摘要（可能含广告或无关内容，请甄别；没有证据的维度如实写"未检索到"）：
{material}

输出 JSON（不要解释）：
{{
  "school_barrier": "宽松|一般|严格|未知",
  "school_note": "学历门槛判断依据一句话（引用搜索证据；无证据则写'未检索到明确学历门槛信息'）",
  "school_tier_friendly": "双非友好|看重院校|未知",
  "school_tier_note": "该校招对双非/民办/普通本科的实际态度一句话（引用面经或录取院校证据）",
  "welfare": ["福利清单（包住宿/餐补/六险一金/班车/房补等，仅收录搜索材料里明确提到的）"],
  "difficulty": "偏高|适中|友好|未知",
  "difficulty_note": "岗位要求难度判断一句话（对照用户技能栈：Python/LangGraph/FastAPI/数据分析）",
  "employee_voice": ["员工口碑要点（加班强度/氛围/待遇，标注来源网站提示）"],
  "recent_moves": ["近期动态（扩张/裁员/经营状况）"],
  "interview_tips": ["面经要点"],
  "w2w_advice": "结合民办二本背景给一句投递建议",
  "confidence": "低|中|高",
  "sources_hint": ["来源网站提示"]
}}
规则：只依据搜索材料，绝不编造；证据越少 confidence 越低。"""

    intel = await _ai_json(prompt)
    if not intel:
        return "⚠️ 公司情报生成失败（搜索/模型暂时不可用），稍后再试"
    intel.setdefault("company", company)
    _save_company_intel(company, intel)
    return _intel_reply_text(company, intel)


# ── 情报批量补全：给高分候选公司批量生成情报卡（后台任务，配额可控） ──
# 目的：让匹配中心的"学历友好/福利"维度有真实区分度——情报覆盖面
# 越大，信号越有区分度。节流 + 状态可查，防止 Tavily/LLM 配额失控。
_intel_backfill: dict = {"running": False, "done": 0, "total": 0,
                         "current": "", "errors": [], "started_at": ""}

_INTEL_BACK_FILL_PAUSE_S = 2   # 每家公司之间停 2 秒（Tavily + LLM 配额保护）


async def _intel_backfill_task(limit: int = 20, min_score: int = 60):
    from job_sentinel.store import get_db as js_get_db
    _intel_backfill.update({"running": True, "done": 0, "total": 0, "current": "",
                            "errors": [], "started_at": _now_local()})
    logger.info(f"情报批量补全开始：limit={limit}, min_score={min_score}")
    try:
        with js_get_db(str(DB_PATH)) as c:
            rows = c.execute(
                "SELECT company, MAX(match_score) AS ms FROM jobs WHERE user_hidden=0 "
                "GROUP BY company HAVING ms >= ? ORDER BY ms DESC LIMIT ?",
                (min_score, limit * 2),
            ).fetchall()
        # 只调研"没有新鲜情报"的公司（缓存 7 天内有效 + 新 schema 才跳过）
        targets = []
        for r in rows:
            co = (r["company"] or "").strip()
            cached = _load_company_intel(co)
            if _intel_fresh(cached):  # 旧 schema 卡不算新鲜，会自动重研升级
                continue
            targets.append((co, r["ms"]))
            if len(targets) >= limit:
                break
        _intel_backfill["total"] = len(targets)
        for co, ms in targets:
            if _SHUTDOWN.is_set():
                break
            _intel_backfill["current"] = co
            try:
                await company_research(co)
                _intel_backfill["done"] += 1
            except Exception as e:  # noqa: BLE001
                _intel_backfill["errors"].append(f"{co}: {e}")
                _intel_backfill["done"] += 1
            await asyncio.sleep(_INTEL_BACK_FILL_PAUSE_S)
        logger.info(
            f"情报批量补全完成：{_intel_backfill['done']}/{_intel_backfill['total']}，"
            f"错误 {len(_intel_backfill['errors'])}"
        )
    except Exception as e:  # noqa: BLE001
        _intel_backfill["running"] = False
        logger.warning(f"情报批量补全异常终止: {e}")
    finally:
        _intel_backfill["running"] = False


async def daily_intel_backfill(limit: int = 50, min_score: int = 65) -> dict:
    """每日自动补全入口（调度器调用）：Top 候选公司补情报，已在跑则跳过。

    与手动 /api/intel/backfill 共用状态字典与任务；每天最多触发一次。
    返回触发情况，供调度器日志记录。
    """
    if _intel_backfill.get("running"):
        logger.info("每日情报补全跳过：上一轮批量调研仍在进行")
        return {"triggered": False, "reason": "running"}
    logger.info(f"每日情报自动补全启动：Top{limit}（min_score={min_score}）")
    asyncio.create_task(_intel_backfill_task(limit, min_score))
    return {"triggered": True, "limit": limit, "min_score": min_score}


# ── 路由 ──────────────────────────────────────────────────────

@router.get("/api/company-intel", dependencies=[Depends(require_auth)])
async def api_company_intel(company: str, refresh: bool = False, focus: str = ""):
    company = (company or "").strip()
    if not company:
        raise HTTPException(status_code=400, detail="缺少 company 参数")
    cached = _load_company_intel(company)
    if cached and not refresh and _intel_fresh(cached):
        return {"company": company, "intel": cached["intel"],
                "searched_at": cached["searched_at"], "cached": True}
    await company_research(company, focus)
    fresh = _load_company_intel(company)
    return {"company": company, "intel": (fresh or {}).get("intel", {}),
            "searched_at": (fresh or {}).get("searched_at", ""), "cached": False}


@router.post("/api/intel/backfill", dependencies=[Depends(require_auth)])
async def api_intel_backfill(limit: int = 20, min_score: int = 60):
    """启动批量情报补全（后台执行）。配额参考：20 家 ≈ 160 次 Tavily + 20 次 LLM。"""
    if _intel_backfill.get("running"):
        raise HTTPException(status_code=409, detail="批量调研已在进行中，请等它结束")
    asyncio.create_task(_intel_backfill_task(limit, min_score))
    return {"started": True, "limit": limit, "min_score": min_score}


@router.get("/api/intel/backfill/status")
async def api_intel_backfill_status():
    return {**_intel_backfill,
            "tavily_configured": bool(os.getenv("TAVILY_API_KEY"))}
