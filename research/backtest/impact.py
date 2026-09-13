"""摩擦与流动性冲击精算模型 (历史研究兼容).

实现跟单交易的单边手续费扣除、池深滑点冲击精算，以及大V自身账面收益的计算。
纯数学与只读精算模型，零外部网络与执行依赖。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FrictionModel:
    """摩擦与滑点冲击计算器."""

    fee_rate: float = 0.006  # 单边手续费 0.6%
    impact_factor: float = 0.5  # 池深冲击乘数 0.5
    low_liquidity_threshold: float = 50000.0  # 低流动性红线 $50k

    def calculate_follow_return(
        self,
        entry_price: float,
        exit_price: float,
        trade_size_usd: float,
        pool_liquidity_usd: float,
    ) -> tuple[float, float, float]:
        """计算跟单买入净价、卖出净价与跟单实际收益率.

        :param entry_price: 跟单买入行情价
        :param exit_price: 跟单卖出行情价
        :param trade_size_usd: 跟单交易名义额 (USD)
        :param pool_liquidity_usd: 池子流动性储备 (USD)
        :return: (net_entry_price, net_exit_price, R_follow)
        """
        if entry_price <= 0 or exit_price <= 0:
            return 0.0, 0.0, 0.0

        # 1. 基础单边手续费
        net_entry = entry_price * (1.0 + self.fee_rate)
        net_exit = exit_price * (1.0 - self.fee_rate)

        # 2. 池深冲击精算: trade_size / liquidity * 0.5 系数 (最大不超过 50% 保护)
        if pool_liquidity_usd > 0:
            impact = min((trade_size_usd / pool_liquidity_usd) * self.impact_factor, 0.5)
        else:
            impact = 0.05  # 无流动性数据时默认 5% 冲击

        net_entry *= 1.0 + impact
        net_exit *= 1.0 - impact

        if net_exit < 0:
            net_exit = 0.0

        if net_entry <= 0:
            return net_entry, net_exit, 0.0

        r_follow = (net_exit - net_entry) / net_entry
        return net_entry, net_exit, r_follow

    def is_low_liquidity(self, liquidity_usd: float) -> bool:
        """判定池子是否属于低流动性高风险池 (< $50k)."""
        return liquidity_usd < self.low_liquidity_threshold

    @staticmethod
    def calculate_boss_return(
        entry_usd: float, exit_usd: float, token_amount: float
    ) -> tuple[float, float, float]:
        """计算大V账面开仓价、平仓价与收益率.

        :return: (boss_entry_price, boss_exit_price, boss_return)
        """
        if token_amount <= 0 or entry_usd <= 0:
            return 0.0, 0.0, 0.0
        pe = entry_usd / token_amount
        px = exit_usd / token_amount
        r = (px - pe) / pe
        return pe, px, r


__all__ = [
    "FrictionModel",
]
