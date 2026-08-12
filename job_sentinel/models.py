"""Job 数据模型 —— 秋招哨兵核心实体。

字段对齐主流校招聚合站（offer多多 / resume maker offer / 国聘）的表格结构：
公司、岗位、城市、学历、批次(提前批/秋招/春招/补录/实习)、截止日、投递链接、
性质(央企/国企/民企/外企)、行业、专业要求、院校要求、薪资。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict

VALID_STATUS = ["新", "已看", "已投", "笔试", "面试", "挂", "OC"]
VALID_BATCH = ["提前批", "秋招", "春招", "补录", "实习", "其他"]


@dataclass
class Job:
    company: str
    title: str
    city: str = ""
    education: str = ""          # 学历要求，如 "本科及以上" / "硕士及以上"
    batch: str = ""              # 提前批/秋招/春招/补录/实习
    deadline: str = ""           # 截止日，如 "2026-09-08" / "招满即止"
    url: str = ""                # 投递链接（经 company_links.resolve_url 解析后必定可跳转）
    link_type: str = ""          # 链接质量：官方校招页 / 源详情页 / 搜索直达
    source: str = ""             # 来源站，如 "offer多多"
    nature: str = ""             # 央企/国企/民企/外企
    industry: str = ""           # 行业
    major: str = ""              # 专业要求
    school_level: str = ""       # 院校要求，如 "双非可投"
    salary: str = ""             # 薪资
    first_seen: str = ""         # 首次发现日期
    match_score: int = 0         # 匹配分 0-100
    match_reason: str = ""       # 一句话推荐理由
    status: str = "新"           # 投递状态
    raw_hash: str = ""           # 去重指纹

    def dedup_hash(self) -> str:
        base = f"{self.company}|{self.title}|{self.city}".strip().lower()
        return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
