"""扫描编排 —— 串起 采集 → 去重 → 硬过滤 → 打分 → 入库。

调用示例：
    res = await scan_and_match(db_path="hermes.db", profile_path="job_sentinel/profile.json",
                                seed_path="job_sentinel/seed_jobs.json")
"""
from __future__ import annotations

import asyncio

from . import sources as src_mod
from .company_links import resolve_url
from .matcher import (hard_filter, score_job, recognize_batch,
                      canon_enterprise_type, detect_exam_free)
from .models import Job
from .profile import load_profile
from .store import get_db, upsert_job, job_stats, prune_invalid

# 这些链接类型由采集源直接给出，已指向具体岗位/简章，优于公司首页，不再二次解析
TRUSTED_LINK_TYPES = ("官方网申", "简章详情")


async def _safe_fetch(s, profile) -> list[Job]:
    try:
        return await s.fetch(profile)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{type(e).__name__}: {e}") from e


async def scan_and_match(db_path, profile_path, seed_path=None) -> dict:
    profile = load_profile(profile_path)
    srcs = src_mod.get_all_sources(seed_path)

    # 1. 各源并发采集（任一失败不影响其他）
    collected: list[Job] = []
    src_stats: dict = {}
    results = await asyncio.gather(
        *[_safe_fetch(s, profile) for s in srcs], return_exceptions=True
    )
    for s, res in zip(srcs, results):
        if isinstance(res, Exception):
            src_stats[s.name] = f"失败:{res}"
        else:
            src_stats[s.name] = f"抓到{len(res)}条"
            collected.extend(res)

    # 2. 去重 + 硬过滤 + 规则打分
    seen: set = set()
    kept: list[Job] = []
    for j in collected:
        h = j.dedup_hash()
        if h in seen:
            continue
        seen.add(h)
        if not hard_filter(j, profile):
            continue
        j.batch = recognize_batch(j.batch or f"{j.title} {j.industry}")
        # 企业性质归一化：源直接给的 enterprise_type 优先，否则从 nature 原文推断
        if not j.enterprise_type:
            j.enterprise_type = canon_enterprise_type(j.nature)
        # 笔试标注：从 JD/标题/行业文本检测（源已标则保留）
        if not j.exam_free:
            j.exam_free = detect_exam_free(
                f"{j.title} {j.industry} {j.major} {j.jd_text}"
            )
        # 修正投递链接：聚合源常给自家分页地址，点不到公司。
        # 但若采集源已给出岗位级直链（网申表单 / 招聘简章页），它比公司首页
        # 精准得多，不能被覆盖。
        if j.link_type not in TRUSTED_LINK_TYPES or not j.url:
            j.url, j.link_type = resolve_url(j.company, j.url)
        sc, reason = score_job(j, profile)
        j.match_score = sc
        j.match_reason = reason
        kept.append(j)

    # 3. 入库（已存在的只更新分数/状态，不重复计为新岗）
    new_count = 0
    with get_db(db_path) as c:
        # 先清掉历史噪声：规则收紧后旧库里不合格的、以及改造前遗留的
        # 无链接质量标记（link_type 为空 = 链接未经解析，多半点不到公司）的条目
        pruned = prune_invalid(
            c, lambda job: bool(job.link_type) and hard_filter(job, profile)
        )
        for j in kept:
            before = c.execute("SELECT id FROM jobs WHERE raw_hash=?", (j.dedup_hash(),)).fetchone()
            upsert_job(c, j)
            if before is None:
                new_count += 1
        stats = job_stats(c)

    return {
        "profile": profile,
        "source_stats": src_stats,
        "collected": len(collected),
        "kept": len(kept),
        "new": new_count,
        "pruned": pruned,
        "stats": stats,
        "top": [j.to_dict() for j in sorted(kept, key=lambda x: -x.match_score)[:10]],
    }
