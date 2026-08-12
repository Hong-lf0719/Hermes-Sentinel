"""用户画像配置 —— 决定哨兵抓什么、匹配什么。

存 profile.json，Web 设置页一个表单即可改。改完即时生效。
这是用户要的「可选地区 / 方向 / 学历 / 学校 / 批次」的落点。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_PROFILE: dict = {
    "regions": ["武汉", "孝感", "湖北", "远程"],
    "directions": ["AI应用开发", "数据开发", "大模型应用", "数据分析", "后端开发"],
    "education": "本科",
    "school_level": "双非可投",
    "batches": ["提前批", "秋招", "春招", "补录", "实习"],
    "salary_min": 0,
    "exclude_keywords": [],
    "updated_at": "",
}


def load_profile(path) -> dict:
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
            merged = dict(DEFAULT_PROFILE)
            merged.update({k: v for k, v in data.items() if k in DEFAULT_PROFILE})
            return merged
        except Exception:
            pass
    return dict(DEFAULT_PROFILE)


def save_profile(profile: dict, path) -> dict:
    merged = dict(DEFAULT_PROFILE)
    merged.update({k: v for k, v in profile.items() if k in DEFAULT_PROFILE})
    merged["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    Path(path).write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
    return merged
