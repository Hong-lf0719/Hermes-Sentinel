"""用户画像配置 —— 决定哨兵抓什么、匹配什么、给谁加权。

存 profile.json，Web 设置页一个表单即可改。改完即时生效。
选项均来自岗位库真实分布（见 store.get_profile_options），用户「勾选」而非「瞎填」。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_PROFILE: dict = {
    "regions": ["武汉", "孝感", "湖北", "远程"],
    # 求职方向：canonical 方向类别（见 matcher.DIRECTION_CATEGORIES）
    "directions": ["AI/大模型/算法", "数据开发/分析", "软件开发(后端)"],
    "recruit_type": [],          # 招聘类型：校招/社招/实习/见习（空=不限）
    "nature": [],                # 单位性质 canonical：央企/国企/民企/外企/事业单位/上市
    "edu_threshold": "不限",      # 学历门槛（单选，看板层过滤，默认不过滤）：不限/本科可投/需硕士
    "major": [],                 # 专业 canonical：计算机类/软件工程/电子信息/通信/自动化/数据科学人工智能
    "education": "本科",          # 用户自身学历层次（参考，不打分用）
    "batches": ["提前批", "秋招", "春招", "补录", "实习"],
    "salary_min": 0,
    "exclude_keywords": [],
    # 标题降权词：命中 -12（如"算法"——避开纯算法岗，但不硬删保留人工判断）
    "downgrade_keywords": [],
    # ── 匹配中心偏好（company_match 打分用）──
    "accept_overtime": "中",       # 加班接受度：高/中/低（与情报口碑联动打分）
    "priority": "稳定",            # 优先级：稳定/成长/薪资（影响动态类信号解读）
    "industry_pref": [],           # 行业偏好关键词（命中公司行业字段即加分）
    "updated_at": "",
}

# 旧方向标签 → 新类别（升级时自动迁移，避免旧画像分数归零）
_DIR_MIGRATE = {
    "AI应用开发": "AI/大模型/算法", "大模型应用": "AI/大模型/算法",
    "数据开发": "数据开发/分析", "数据分析": "数据开发/分析",
    "后端开发": "软件开发(后端)",
}


def _migrate(p: dict) -> dict:
    """把旧版画像字段平滑迁移到新结构。"""
    if "directions" in p:
        seen, migrated = set(), []
        for d in p["directions"] or []:
            nd = _DIR_MIGRATE.get(d, d)
            if nd not in seen:
                seen.add(nd); migrated.append(nd)
        p["directions"] = migrated
    # 旧院校等级字段 → 学历门槛（单选默认值）
    if "edu_threshold" not in p and p.get("school_tier"):
        p["edu_threshold"] = "本科可投"
    # 丢弃已废弃字段
    for k in ("prefer_companies", "prefer_natures", "school_level", "school_tier"):
        p.pop(k, None)
    return p


def load_profile(path) -> dict:
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
            merged = dict(DEFAULT_PROFILE)
            merged.update({k: v for k, v in data.items() if k in DEFAULT_PROFILE})
            return _migrate(merged)
        except Exception:
            pass
    return dict(DEFAULT_PROFILE)


def save_profile(profile: dict, path) -> dict:
    merged = dict(DEFAULT_PROFILE)
    merged.update({k: v for k, v in profile.items() if k in DEFAULT_PROFILE})
    _migrate(merged)
    merged["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    Path(path).write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
    return merged
