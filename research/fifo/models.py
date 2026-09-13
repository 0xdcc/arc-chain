"""FIFO 撮合与回测基础数据结构 (历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块数据结构移植自原回测套件与离线研究上游：
- 上游来源 1: /root/projects/crypto/v1-legacy/dex-sniper-engine/backtest/data/ingesters/base.py
  (SHA-256: 86ed15d8b19c13382ae7898046ee9c7204c678174623eb7c9872e5885b5c2c77)
- 上游来源 2: /root/projects/crypto/v2-modular/dex-sniper-engine-modular/backtest/pipeline/matcher.py
  (SHA-256: cc1204f5d239869a4dfd9d2d84ae7ddba124f776f42e89a57aa90b2192d07909)

本模块字段使用 float（浮点数）如实保留历史研究回测算法语义，专供纯离线回测、
持仓切片分析与因果复盘使用。
严禁接入生产链上金融执行（execution）整数路径；不借任务改动精度算法与清洗策略。
零网络、零 RPC、零执行器、零 4663 池目录耦合。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SwapRecord:
    """标准化的单笔 Swap 流水记录 (历史研究兼容)."""

    trader: str  # 钱包/交易员标识
    token: str  # 标的代币地址 (统一转小写)
    side: str  # "buy" | "sell"
    ts: float  # Unix 时间戳 (秒)
    usd_amount: float  # USD 名义额
    token_amount: float  # 代币数量
    raw: dict[str, Any] = field(default_factory=dict)  # 原始记录 (供追溯)
    unsolicited: bool = False  # 是否为外部代买/空投转入标记


@dataclass
class ClosedPair:
    """已平仓交易对 (历史研究兼容)."""

    trader: str
    token: str
    entry_ts: float
    exit_ts: float
    entry_usd: float
    exit_usd: float
    token_amount: float
    hold_seconds: float
    unsolicited: bool = False
    pool: str | None = None
    pool_liquidity: float = 0.0
    low_liquidity: bool = False

    # 大V账面收益字段
    boss_entry_price: float = 0.0
    boss_exit_price: float = 0.0
    boss_return: float = 0.0

    # 附加元数据
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.token_amount > 0:
            if self.boss_entry_price <= 0 and self.entry_usd > 0:
                self.boss_entry_price = self.entry_usd / self.token_amount
            if self.boss_exit_price <= 0 and self.exit_usd >= 0:
                self.boss_exit_price = self.exit_usd / self.token_amount
        if self.boss_entry_price > 0:
            self.boss_return = (
                self.boss_exit_price - self.boss_entry_price
            ) / self.boss_entry_price
        elif self.entry_usd > 0:
            self.boss_return = (self.exit_usd - self.entry_usd) / self.entry_usd


@dataclass
class OpenPosition:
    """未平仓持仓 (历史研究兼容)."""

    trader: str
    token: str
    entry_ts: float
    token_amount: float
    entry_usd: float
    unsolicited: bool = False
