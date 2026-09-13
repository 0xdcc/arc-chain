"""数据清洗管道 (防坑 4/6 全套规则，历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块数据清洗规则与阈值移植自原回测套件与离线研究上游：
- 上游来源: /root/projects/crypto/v2-modular/dex-sniper-engine-modular/backtest/pipeline/cleaner.py
  (SHA-256: 8d2175840fc550449d4cd0778554e2522766ad34f59fb3ce2660c038653903ff)

本模块清洗阈值与算法如实保留历史研究清洗语义，专供纯离线回测、持仓切片分析与因果复盘使用。
严禁接入 Arc 交易盈利判据或新增生产经济 gate；不恢复 4663 池、RPC、网络或执行器依赖。
零网络、零 RPC、零私钥、零执行器。
"""

from dataclasses import dataclass

from research.fifo.models import ClosedPair


@dataclass
class CleanerConfig:
    """清洗规则阈值配置."""

    min_unit_price: float | None = None  # 规则 1: 单价下限 (如 0.01)
    min_cost: float = 20.0  # 规则 2: 成本下限 ($20)
    max_return: float = 20.0  # 规则 3: 收益率上限 (2000%)
    max_price_deviation: float = 5.0  # 规则 4: 偏离倍数上限 (5倍)
    fallback_friction_target: float = -0.017  # 规则 5: fallback 假价格特征值 (-1.7%)
    fallback_friction_tolerance: float = 0.005  # 规则 5: 假价格容差 (|R - (-0.017)| < 0.005)


class Cleaner:
    """交易对清洗器."""

    def __init__(self, config: CleanerConfig | None = None) -> None:
        self.config = config or CleanerConfig()

    def is_low_unit_price(self, pair: ClosedPair) -> bool:
        """规则 1: 判定单价是否低于阈值 (< $0.01)."""
        if self.config.min_unit_price is None:
            return False
        return pair.boss_entry_price < self.config.min_unit_price

    def is_low_cost(self, pair: ClosedPair) -> bool:
        """规则 2: 判定成本是否过低 (< $20)."""
        return pair.entry_usd < self.config.min_cost

    def is_extreme_return(self, pair: ClosedPair) -> bool:
        """规则 3: 判定收益率是否极端异常 (> 2000% 或 < -99%)."""
        return pair.boss_return > self.config.max_return or pair.boss_return < -0.99

    def is_price_deviated(
        self,
        pair: ClosedPair,
        market_entry_price: float | None = None,
        market_exit_price: float | None = None,
    ) -> bool:
        """规则 4: 判定成交价 vs 市场价偏离是否 > 5 倍."""
        max_dev = self.config.max_price_deviation
        if market_entry_price is not None and market_entry_price > 0 and pair.boss_entry_price > 0:
            dev_in = max(
                market_entry_price / pair.boss_entry_price,
                pair.boss_entry_price / market_entry_price,
            )
            if dev_in > max_dev:
                return True

        if market_exit_price is not None and market_exit_price > 0 and pair.boss_exit_price > 0:
            dev_out = max(
                market_exit_price / pair.boss_exit_price,
                pair.boss_exit_price / market_exit_price,
            )
            if dev_out > max_dev:
                return True

        return False

    def is_fallback_fake_price(self, return_rate: float) -> bool:
        """规则 5: 判定是否为 fallback 假价格 (|R - (-0.017)| < 0.005)."""
        return (
            abs(return_rate - self.config.fallback_friction_target)
            < self.config.fallback_friction_tolerance
        )

    def clean_pre_pricing(self, pairs: list[ClosedPair]) -> tuple[list[ClosedPair], dict[str, int]]:
        """执行计价前的清洗 (规则 1, 2, 3, 6).

        :return: (保留的对子列表, 各规则剔除统计)
        """
        kept: list[ClosedPair] = []
        drops: dict[str, int] = {
            "low_unit_price": 0,
            "low_cost": 0,
            "extreme_return": 0,
            "unsolicited_flagged": 0,
        }

        for p in pairs:
            # 规则 6: unsolicited 打标 (保留记录但不计入胜率统计)
            if p.unsolicited:
                drops["unsolicited_flagged"] += 1

            # 规则 1: 单价 < $0.01
            if self.is_low_unit_price(p):
                drops["low_unit_price"] += 1
                continue

            # 规则 2: 成本 < $20
            if self.is_low_cost(p):
                drops["low_cost"] += 1
                continue

            # 规则 3: 收益 > 2000%
            if self.is_extreme_return(p):
                drops["extreme_return"] += 1
                continue

            kept.append(p)

        return kept, drops

    def filter_fake_prices(
        self, pairs_with_returns: list[tuple[ClosedPair, float]]
    ) -> tuple[list[tuple[ClosedPair, float]], int]:
        """执行计价后的假价格过滤 (规则 5).

        :param pairs_with_returns: (ClosedPair, R_follow) 列表
        :return: (有效对子列表, 剔除的假价格数量)
        """
        valid: list[tuple[ClosedPair, float]] = []
        dropped_fake_count = 0

        for pair, r_follow in pairs_with_returns:
            if self.is_fallback_fake_price(r_follow):
                dropped_fake_count += 1
                continue
            valid.append((pair, r_follow))

        return valid, dropped_fake_count
