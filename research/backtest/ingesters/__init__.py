"""流水适配器注册表与工厂方法 (历史研究兼容)."""

from research.backtest.ingesters.base import BaseIngester, SwapRecord
from research.backtest.ingesters.fomo import FomoIngester
from research.backtest.ingesters.generic_csv import GenericCSVIngester

_INGESTERS: dict[str, type[BaseIngester]] = {
    "fomo": FomoIngester,
    "generic_csv": GenericCSVIngester,
    "csv": GenericCSVIngester,
}


def register_ingester(name: str, ingester_cls: type[BaseIngester]) -> None:
    """注册新的流水适配器类."""
    _INGESTERS[name.lower()] = ingester_cls


def get_ingester(name: str, **kwargs: object) -> BaseIngester:
    """获取指定名称的流水适配器实例."""
    key = name.lower()
    if key not in _INGESTERS:
        raise ValueError(f"未知的 ingester: {name} (已注册: {list(_INGESTERS.keys())})")
    cls = _INGESTERS[key]
    return cls(**kwargs)


__all__ = [
    "BaseIngester",
    "SwapRecord",
    "FomoIngester",
    "GenericCSVIngester",
    "register_ingester",
    "get_ingester",
]
