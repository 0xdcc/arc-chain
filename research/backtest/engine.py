"""矩阵回测执行器 (金额 × 延迟，历史研究兼容).

【历史研究兼容性声明 / Historical Research Compatibility Notice】
本模块算法移植自原回测套件与离线研究上游：
- 上游来源: /root/projects/crypto/v2-modular/dex-sniper-engine-modular/backtest/engine.py
  (SHA-256: c9d55529f798e162f1ff7a6e1dbe9b85c13e51f8fd6eb10f1fb2587bb3cbe4e6)

执行 (金额档 × 延迟档) 完整参数网格回测，
采用中位数与胜率作为首要评价标准，输出多维分组诊断数据。
专供纯离线回测与历史因果复盘使用。
零网络、零 RPC、零私钥、零执行器。
"""

from __future__ import annotations

import statistics as st
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from research.backtest.config import BacktestConfig
from research.cleaner import Cleaner, CleanerConfig
from research.fifo import ClosedPair, FIFOMatcher, SwapRecord

if TYPE_CHECKING:
    from research.backtest.impact import FrictionModel
    from research.backtest.price_cache import PriceCache

# 分交易员结论所需的最小样本量（低于此值只标注“样本不足”，不下可跟/别跟结论）
MIN_TRADER_SAMPLE = 30


@dataclass
class EvaluatedPair:
    """完成跟单定价与摩擦精算的交易对记录."""

    pair: ClosedPair
    amount: float
    delay: int
    entry_price_follow: float
    exit_price_follow: float
    net_entry_price: float
    net_exit_price: float
    follow_return: float
    boss_return: float


@dataclass
class GroupStats:
    """分组统计数据."""

    name: str
    n_pairs: int = 0
    boss_median_return: float = 0.0
    boss_win_rate: float = 0.0
    follow_median_return: float = 0.0
    follow_win_rate: float = 0.0
    follow_mean_return: float = 0.0
    recommendation: str = ""


@dataclass
class BacktestResult:
    """回测结果集."""

    config: BacktestConfig
    raw_swaps_count: int = 0
    matched_pairs_count: int = 0
    open_positions_count: int = 0
    pre_clean_drops: dict[str, int] = field(default_factory=dict)
    clean_pairs_count: int = 0
    no_trade_count: int = 0

    # 矩阵统计: (amount, delay) -> stats dict
    matrix_stats: dict[tuple[float, int], dict[str, Any]] = field(default_factory=dict)

    # 默认基准配置下的多维分组
    baseline_amount: float = 300.0
    baseline_delay: int = 3
    trader_groups: list[GroupStats] = field(default_factory=list)
    duration_groups: list[GroupStats] = field(default_factory=list)
    liquidity_groups: list[GroupStats] = field(default_factory=list)

    # 原始明细 (可供深入追踪)
    evaluated_pairs_baseline: list[EvaluatedPair] = field(default_factory=list)


class BacktestEngine:
    """跨链跟单矩阵回测执行引擎."""

    def __init__(
        self,
        config: BacktestConfig | None = None,
        price_cache: PriceCache | Any | None = None,
        friction_model: FrictionModel | Any | None = None,
        cleaner: Cleaner | None = None,
        matcher: FIFOMatcher | None = None,
        tick_cache: Any | None = None,
    ) -> None:
        self.config = config or BacktestConfig()

        if price_cache is not None:
            self.price_cache = price_cache
        else:
            try:
                from research.backtest.price_cache import PriceCache
                self.price_cache = PriceCache()
            except Exception:
                self.price_cache = None

        # TickCache 为可选依赖，离线环境下不默认初始化真实 RPC 实例，保留明确边界
        self.tick_cache = tick_cache

        if friction_model is not None:
            self.friction_model = friction_model
        else:
            try:
                from research.backtest.impact import FrictionModel
                self.friction_model = FrictionModel(
                    fee_rate=self.config.fee_rate,
                    impact_factor=self.config.impact_factor,
                    low_liquidity_threshold=self.config.low_liquidity_threshold,
                )
            except Exception:
                self.friction_model = None

        self.cleaner = cleaner or Cleaner(
            CleanerConfig(
                min_unit_price=self.config.min_unit_price,
                min_cost=self.config.min_cost,
                max_return=self.config.max_return,
                max_price_deviation=self.config.max_price_deviation,
                fallback_friction_target=self.config.fallback_friction_target,
                fallback_friction_tolerance=self.config.fallback_friction_tolerance,
            )
        )
        self.matcher = matcher or FIFOMatcher(merge_partial=True)

    def run(
        self,
        records: list[SwapRecord] | None = None,
        pairs: list[ClosedPair] | None = None,
    ) -> BacktestResult:
        """执行完整回测流程."""
        if self.config.use_tick and self.tick_cache is None:
            raise RuntimeError(
                "use_tick=True is blocked: TickCache is an optional dependency and was not provided. "
                "Silently ignoring use_tick or fabricating tick data is strictly prohibited."
            )

        if records is not None and pairs is None:
            raw_count = len(records)
            closed_pairs, open_positions = self.matcher.match(records)
            open_count = len(open_positions)
        elif pairs is not None:
            raw_count = len(pairs)
            closed_pairs = pairs
            open_count = 0
        else:
            raise ValueError("必须提供 records 或 pairs 输入")

        matched_count = len(closed_pairs)

        # 1. 补全流动性池信息
        for p in closed_pairs:
            if not p.pool:
                if self.price_cache is not None and hasattr(self.price_cache, "find_best_pool"):
                    p_addr, p_liq = self.price_cache.find_best_pool(p.token)
                    p.pool = p_addr
                    p.pool_liquidity = p_liq
                    if self.friction_model is not None and hasattr(self.friction_model, "is_low_liquidity"):
                        p.low_liquidity = self.friction_model.is_low_liquidity(p_liq)

        # 2. 计价前清洗 (规则 1, 2, 3, 6)
        clean_pairs, pre_clean_drops = self.cleaner.clean_pre_pricing(closed_pairs)
        clean_count = len(clean_pairs)

        # 3. 矩阵回测 (amounts × delays)
        matrix_stats: dict[tuple[float, int], dict[str, Any]] = {}
        all_evaluated: dict[tuple[float, int], list[EvaluatedPair]] = {}

        for amount in self.config.trade_amounts:
            for delay in self.config.delays:
                stats, evaluated = self._evaluate_single_cell(clean_pairs, amount, delay)
                matrix_stats[(amount, delay)] = stats
                all_evaluated[(amount, delay)] = evaluated

        # 4. 选择默认基准档位进行多维分组诊断
        baseline_amount = (
            300.0 if 300.0 in self.config.trade_amounts else self.config.trade_amounts[0]
        )
        baseline_delay = 3 if 3 in self.config.delays else self.config.delays[0]
        baseline_evaluated = all_evaluated.get((baseline_amount, baseline_delay), [])

        trader_groups = self._group_by_trader(baseline_evaluated)
        duration_groups = self._group_by_duration(baseline_evaluated)
        liquidity_groups = self._group_by_liquidity(baseline_evaluated)

        baseline_stats_dict = matrix_stats.get((baseline_amount, baseline_delay), {})
        baseline_no_trade = int(baseline_stats_dict.get("no_trade_count", 0))

        return BacktestResult(
            config=self.config,
            raw_swaps_count=raw_count,
            matched_pairs_count=matched_count,
            open_positions_count=open_count,
            pre_clean_drops=pre_clean_drops,
            clean_pairs_count=clean_count,
            no_trade_count=baseline_no_trade,
            matrix_stats=matrix_stats,
            baseline_amount=baseline_amount,
            baseline_delay=baseline_delay,
            trader_groups=trader_groups,
            duration_groups=duration_groups,
            liquidity_groups=liquidity_groups,
            evaluated_pairs_baseline=baseline_evaluated,
        )

    def _evaluate_single_cell(
        self, pairs: list[ClosedPair], amount: float, delay: int
    ) -> tuple[dict[str, Any], list[EvaluatedPair]]:
        evaluated: list[EvaluatedPair] = []
        missing_count = 0
        polluted_count = 0
        dev_dropped_count = 0
        no_trade_count = 0

        for pair in pairs:
            if not pair.pool:
                missing_count += 1
                continue

            if self.config.use_tick:
                if self.tick_cache is None:
                    raise RuntimeError(
                        "use_tick=True is blocked: TickCache is an optional dependency and was not provided."
                    )
                p_entry, meta_entry = self.tick_cache.price_at_with_meta(
                    pair.pool, pair.entry_ts + delay
                )
                p_exit, meta_exit = self.tick_cache.price_at_with_meta(
                    pair.pool, pair.exit_ts + delay
                )

                # 关键语义: 容差内无成交不计入收益，单独打标计入 no_trade_count
                if meta_entry == "no_trade_at_signal" or meta_exit == "no_trade_at_signal":
                    no_trade_count += 1
                    continue

                if p_entry is None or p_exit is None or p_entry <= 0 or p_exit <= 0:
                    missing_count += 1
                    continue
            else:
                if self.price_cache is None:
                    raise RuntimeError(
                        "PriceCache is required when use_tick=False, but none was provided or resolved"
                    )
                # 跟单买入价与卖出价
                p_entry = self.price_cache.price_at(pair.pool, pair.entry_ts + delay)
                p_exit = self.price_cache.price_at(pair.pool, pair.exit_ts + delay)

                if p_entry is None or p_exit is None or p_entry <= 0 or p_exit <= 0:
                    missing_count += 1
                    continue

            # 规则 4: 成交价偏离 > 5倍 剔除
            if self.cleaner.is_price_deviated(
                pair, market_entry_price=p_entry, market_exit_price=p_exit
            ):
                dev_dropped_count += 1
                continue

            # 计算扣除手续费与池深冲击后的净价
            if self.friction_model is None:
                raise RuntimeError(
                    "FrictionModel is required for return calculation, but none was provided or resolved"
                )
            net_entry, net_exit, r_follow = self.friction_model.calculate_follow_return(
                p_entry, p_exit, amount, pair.pool_liquidity
            )

            # 规则 5: fallback 假价格污染检测
            if self.cleaner.is_fallback_fake_price(r_follow):
                polluted_count += 1
                continue

            evaluated.append(
                EvaluatedPair(
                    pair=pair,
                    amount=amount,
                    delay=delay,
                    entry_price_follow=p_entry,
                    exit_price_follow=p_exit,
                    net_entry_price=net_entry,
                    net_exit_price=net_exit,
                    follow_return=r_follow,
                    boss_return=pair.boss_return,
                )
            )

        n_pairs = len(evaluated)
        total_attempts = len(pairs)
        missing_price_rate = (missing_count / total_attempts) if total_attempts > 0 else 0.0

        if n_pairs == 0:
            empty_stats = {
                "n_pairs": 0,
                "boss_median_return": 0.0,
                "boss_win_rate": 0.0,
                "follow_median_return": 0.0,
                "follow_win_rate": 0.0,
                "follow_mean_return": 0.0,
                "return_decay": 0.0,
                "median_hold_seconds": 0.0,
                "max_single_loss": 0.0,
                "missing_price_rate": missing_price_rate,
                "polluted_filtered": polluted_count,
                "no_trade_count": no_trade_count,
            }
            return empty_stats, evaluated

        boss_returns = [ep.boss_return for ep in evaluated]
        follow_returns = [ep.follow_return for ep in evaluated]
        hold_times = [ep.pair.hold_seconds for ep in evaluated]

        # 胜率计算: 排除 unsolicited (规则 6 标记的外部买单)
        solicited_boss = [ep.boss_return for ep in evaluated if not ep.pair.unsolicited]
        solicited_follow = [ep.follow_return for ep in evaluated if not ep.pair.unsolicited]

        boss_win_rate = (
            sum(1 for r in solicited_boss if r > 0) / len(solicited_boss) if solicited_boss else 0.0
        )
        follow_win_rate = (
            sum(1 for r in solicited_follow if r > 0) / len(solicited_follow)
            if solicited_follow
            else 0.0
        )

        boss_med = float(st.median(boss_returns))
        follow_med = float(st.median(follow_returns))
        follow_mean = float(st.mean(follow_returns))
        decay = boss_med - follow_med

        stats = {
            "n_pairs": n_pairs,
            "boss_median_return": boss_med,
            "boss_win_rate": boss_win_rate,
            "follow_median_return": follow_med,
            "follow_win_rate": follow_win_rate,
            "follow_mean_return": follow_mean,
            "return_decay": decay,
            "median_hold_seconds": float(st.median(hold_times)),
            "max_single_loss": float(min(follow_returns)),
            "missing_price_rate": missing_price_rate,
            "polluted_filtered": polluted_count,
            "no_trade_count": no_trade_count,
        }
        return stats, evaluated

    @staticmethod
    def _group_by_trader(evaluated: list[EvaluatedPair]) -> list[GroupStats]:
        groups: dict[str, list[EvaluatedPair]] = {}
        for ep in evaluated:
            groups.setdefault(ep.pair.trader, []).append(ep)

        result: list[GroupStats] = []
        for trader, items in groups.items():
            f_returns = [x.follow_return for x in items]
            b_returns = [x.boss_return for x in items]
            f_solicited = [x.follow_return for x in items if not x.pair.unsolicited]
            b_solicited = [x.boss_return for x in items if not x.pair.unsolicited]

            f_med = float(st.median(f_returns)) if f_returns else 0.0
            f_mean = float(st.mean(f_returns)) if f_returns else 0.0
            b_med = float(st.median(b_returns)) if b_returns else 0.0
            f_win = sum(1 for r in f_solicited if r > 0) / len(f_solicited) if f_solicited else 0.0
            b_win = sum(1 for r in b_solicited if r > 0) / len(b_solicited) if b_solicited else 0.0

            # 样本量保护：少于 MIN_TRADER_SAMPLE 对不给结论，避免小样本误导
            if len(items) < MIN_TRADER_SAMPLE:
                rec = f"⚠️ 样本不足(<{MIN_TRADER_SAMPLE})"
            elif f_med > 0.0 and f_win >= 0.5:
                rec = "🏆 可跟"
            else:
                rec = "❌ 别跟"

            result.append(
                GroupStats(
                    name=trader,
                    n_pairs=len(items),
                    boss_median_return=b_med,
                    boss_win_rate=b_win,
                    follow_median_return=f_med,
                    follow_win_rate=f_win,
                    follow_mean_return=f_mean,
                    recommendation=rec,
                )
            )

        result.sort(key=lambda g: g.follow_median_return, reverse=True)
        return result

    @staticmethod
    def _group_by_duration(evaluated: list[EvaluatedPair]) -> list[GroupStats]:
        buckets: dict[str, list[EvaluatedPair]] = {
            "<1h": [],
            "1h-1d": [],
            "1-7d": [],
            "7-30d": [],
            ">30d": [],
        }
        for ep in evaluated:
            h = ep.pair.hold_seconds
            if h < 3600:
                buckets["<1h"].append(ep)
            elif h < 86400:
                buckets["1h-1d"].append(ep)
            elif h < 7 * 86400:
                buckets["1-7d"].append(ep)
            elif h < 30 * 86400:
                buckets["7-30d"].append(ep)
            else:
                buckets[">30d"].append(ep)

        result: list[GroupStats] = []
        for b_name in ("<1h", "1h-1d", "1-7d", "7-30d", ">30d"):
            items = buckets[b_name]
            if not items:
                result.append(GroupStats(name=b_name, n_pairs=0))
                continue
            f_returns = [x.follow_return for x in items]
            b_returns = [x.boss_return for x in items]
            f_solicited = [x.follow_return for x in items if not x.pair.unsolicited]
            b_solicited = [x.boss_return for x in items if not x.pair.unsolicited]

            result.append(
                GroupStats(
                    name=b_name,
                    n_pairs=len(items),
                    boss_median_return=float(st.median(b_returns)),
                    boss_win_rate=(
                        sum(1 for r in b_solicited if r > 0) / len(b_solicited)
                        if b_solicited
                        else 0.0
                    ),
                    follow_median_return=float(st.median(f_returns)),
                    follow_win_rate=(
                        sum(1 for r in f_solicited if r > 0) / len(f_solicited)
                        if f_solicited
                        else 0.0
                    ),
                    follow_mean_return=float(st.mean(f_returns)),
                )
            )
        return result

    @staticmethod
    def _group_by_liquidity(evaluated: list[EvaluatedPair]) -> list[GroupStats]:
        buckets: dict[str, list[EvaluatedPair]] = {
            "<$50k": [],
            "$50k-500k": [],
            ">$500k": [],
        }
        for ep in evaluated:
            liq = ep.pair.pool_liquidity
            if liq < 50000:
                buckets["<$50k"].append(ep)
            elif liq <= 500000:
                buckets["$50k-500k"].append(ep)
            else:
                buckets[">$500k"].append(ep)

        result: list[GroupStats] = []
        for b_name in ("<$50k", "$50k-500k", ">$500k"):
            items = buckets[b_name]
            if not items:
                result.append(GroupStats(name=b_name, n_pairs=0))
                continue
            f_returns = [x.follow_return for x in items]
            b_returns = [x.boss_return for x in items]
            f_solicited = [x.follow_return for x in items if not x.pair.unsolicited]
            b_solicited = [x.boss_return for x in items if not x.pair.unsolicited]

            result.append(
                GroupStats(
                    name=b_name,
                    n_pairs=len(items),
                    boss_median_return=float(st.median(b_returns)),
                    boss_win_rate=(
                        sum(1 for r in b_solicited if r > 0) / len(b_solicited)
                        if b_solicited
                        else 0.0
                    ),
                    follow_median_return=float(st.median(f_returns)),
                    follow_win_rate=(
                        sum(1 for r in f_solicited if r > 0) / len(f_solicited)
                        if f_solicited
                        else 0.0
                    ),
                    follow_mean_return=float(st.mean(f_returns)),
                )
            )
        return result
