"""采集源注册表。"""
from __future__ import annotations

from .base import SourceAdapter
from .seed import SeedSource
from .givemeoc import GivemeocSource
from .resumemakeroffer import ResumeMakerSource
from .whu_jobs import WhuJobsSource
from .guopin import GuopinSource


def get_all_sources(seed_path=None) -> list[SourceAdapter]:
    """返回全部采集源。

    顺序即优先级：武大就业网给的是「具体岗位 + 简章直链」，质量最高排第一；
    国聘网全国覆盖补外地岗排第二；聚合站只有公司级条目，作补充；种子源最后。
    """
    return [WhuJobsSource(), GuopinSource(), ResumeMakerSource(), GivemeocSource(), SeedSource(seed_path)]
