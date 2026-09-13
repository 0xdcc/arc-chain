"""标准化流水记录与适配器基类 (历史研究兼容).

定义统一的流水导入适配器 BaseIngester，并复用 research.fifo.SwapRecord。
"""

from abc import ABC, abstractmethod
from typing import Any

from research.fifo import SwapRecord


class BaseIngester(ABC):
    """流水导入适配器基类."""

    def __init__(self) -> None:
        self.total_records: int = 0
        self.parsed_records: int = 0
        self.skipped_same_chain: int = 0
        self.skipped_other_chain: int = 0
        self.errors: list[str] = []

    @abstractmethod
    def ingest(self, source: Any) -> list[SwapRecord]:
        """解析输入数据源，返回统一的 SwapRecord 列表."""

    def get_stats(self) -> dict[str, int]:
        """返回解析统计指标."""
        return {
            "total_records": self.total_records,
            "parsed_records": self.parsed_records,
            "skipped_same_chain": self.skipped_same_chain,
            "skipped_other_chain": self.skipped_other_chain,
            "error_count": len(self.errors),
        }


__all__ = [
    "BaseIngester",
    "SwapRecord",
]
