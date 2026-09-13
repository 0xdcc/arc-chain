"""Arc 回测与报告套件 (历史研究兼容).

薄导出回测核心组件，零网络、零 RPC、零私钥、零执行器依赖。
"""

from research.backtest.config import BacktestConfig
from research.backtest.engine import (
    MIN_TRADER_SAMPLE,
    BacktestEngine,
    BacktestResult,
    EvaluatedPair,
    GroupStats,
)
from research.backtest.impact import FrictionModel
from research.backtest.price_cache import PriceCache
from research.backtest.reporter import MarkdownReporter

__all__ = [
    "MIN_TRADER_SAMPLE",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "EvaluatedPair",
    "FrictionModel",
    "GroupStats",
    "MarkdownReporter",
    "PriceCache",
]
