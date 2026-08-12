"""采集源抽象基类 —— 每个数据源一个适配器，统一输出 Job 列表。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Job


class SourceAdapter(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch(self, profile: dict) -> list[Job]:
        """返回该源采集到的岗位列表；任何失败都返回空列表，不影响主流程。"""
        ...
