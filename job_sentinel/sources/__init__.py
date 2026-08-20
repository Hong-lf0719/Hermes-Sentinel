"""采集源注册表。

渠道策略（结合用户画像：2027 数据科学 / 武汉·孝感 / AI应用开发·数据开发）：
  * 自动采集源（服务端、公开、无需登录）：
    - 武汉大学就业网（具体岗位+简章直链，质量最高）
    - 华中科技大学就业网（武汉本地校招补充，已实测 searchJob_N.do 静态分页可达）
    - 国聘网（全国央国企，按城市关键词抓校招、社招已拦截，不再降权）
    - 2 个校招聚合站（公司级补充）、本地种子/目标公司池。
  * 浏览器扩展（用户已登录浏览器内运行，用用户自己的会话，服务端不存账号/cookie）：
    自动捕获牛客/BOSS/猎聘/51job/海投/应届生/各大厂校招官网/ATS 的「网申提交成功页」单次投递，
    并支持「我的投递」列表页批量同步 → 是安全的「渠道倍增器」，覆盖数据/AI/算法岗最集中的牛客等。

安全红线（保证安全）：
  * 不做 BOSS直聘/51job/智联 等反爬重、需登录的服务端硬爬（易封禁、ToS 风险、cookie 易失效）。
  * 高校就业网逐校定制：华科 ≠ 武大平台（华科是 job.hust.edu.cn 静态分页，武大是 xsjy API），
    新增某校须先做端点验证，禁止「猜端点」上线（否则返回 0 条、看起来像坏掉）。
  * 孝感等地方岗：走扩展（用户自行投递即入库）或单独适配，不盲目爬政府站。
"""
from __future__ import annotations

from .base import SourceAdapter
from .seed import SeedSource
from .givemeoc import GivemeocSource
from .resumemakeroffer import ResumeMakerSource
from .whu_jobs import WhuJobsSource
from .hust_jobs import HustJobsSource
from .guopin import GuopinSource
from .shixiseng import ShixisengSource


def get_all_sources(seed_path=None) -> list[SourceAdapter]:
    """返回全部采集源。

    顺序即优先级：武大就业网给的是「具体岗位 + 简章直链」，质量最高排第一；
    华中科技大学就业网武汉本地校招补充排第二；国聘网全国覆盖补外地岗排第三；
    聚合站只有公司级条目，作补充；种子源最后。
    """
    return [WhuJobsSource(), HustJobsSource(), GuopinSource(), ShixisengSource(), ResumeMakerSource(), GivemeocSource(), SeedSource(seed_path)]
