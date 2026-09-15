"""Offline indicative spread analysis; no execution or verified-profit authority."""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from arc_readiness.http_readonly import ArcCircuitBreakerTrippedError as HttpHalt
from research.market_data.multicall import AnyPool, PoolSpec, PriceQuote
from research.market_data.pool_reader import PoolReader
from research.market_data.rpc_pool import ArcCircuitBreakerTrippedError as PoolHalt
from research.market_data.token_policy import is_tax_token
from research.quote_capacity import estimate_realized_profit_usd

__all__ = ["AnyPool", "PoolSpec", "PriceQuote", "PoolReader", "SpreadAlert", "find_spreads", "scan_once", "estimate_realized_profit_usd"]
logger = logging.getLogger(__name__)
MIN_PLAUSIBLE_PRICE_RATIO = 0.9
MAX_PLAUSIBLE_PRICE_RATIO = 1.1
MAX_PLAUSIBLE_SPREAD_PCT = 10.0

@dataclass
class SpreadAlert:
    """跨池套利机会告警."""

    base: str
    quote: str
    buy_pool: AnyPool
    sell_pool: AnyPool
    buy_price: float
    sell_price: float
    gross_spread_pct: float
    total_fee_pct: float
    net_spread_pct: float
    fee_pct: float = 0.0
    bottleneck_tvl: float = 0.0
    max_capacity_usd: float = 0.0
    optimal_size_usd: float = 0.0
    max_profit_usd: float = 0.0
    profit_at_500u: float = 0.0
    base_token: str | None = None
    ts: float = field(default_factory=time.time)
    capacity_source: str = "UNKNOWN"  # MODEL_ESTIMATE is still not executable capacity.

    def __str__(self) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.ts))
        cap_str = (
            f" | 容量: 最优 ${self.optimal_size_usd:,.0f}U "
            f"(500U净赚 +${self.profit_at_500u:.2f}, 顶峰 +${self.max_profit_usd:.2f})"
            if self.optimal_size_usd > 0
            else ""
        )
        return (
            f"[{t}] {self.base}/{self.quote} "
            f"买 {self.buy_pool.label} @{self.buy_price:.6f} → "
            f"卖 {self.sell_pool.label} @{self.sell_price:.6f} | "
            f"毛利 {self.gross_spread_pct:.3f}% - 费用 {self.total_fee_pct:.3f}% "
            f"= 净 {self.net_spread_pct:.3f}%{cap_str}"
        )

def find_spreads(
    quotes: list[PriceQuote],
    slippage_buffer_pct: float = 0.2,
) -> list[SpreadAlert]:
    """在给定报价列表中寻找跨池套利机会."""
    alerts: list[SpreadAlert] = []

    # 按 (base, quote) 分组, 只比同标的
    groups: dict[tuple[str, str], list[PriceQuote]] = {}
    for q in quotes:
        if q.price <= 0 or not math.isfinite(q.price):
            continue
        if not math.isfinite(q.pool.fee_bps) or q.pool.fee_bps < 0:
            continue
        p_t0 = getattr(q.pool, "token0", "").lower()
        p_t1 = getattr(q.pool, "token1", "").lower()
        p_c0 = getattr(q.pool, "currency0", "").lower()
        p_c1 = getattr(q.pool, "currency1", "").lower()
        if (
            is_tax_token(q.base)
            or is_tax_token(q.quote)
            or is_tax_token(p_t0)
            or is_tax_token(p_t1)
            or is_tax_token(p_c0)
            or is_tax_token(p_c1)
        ):
            continue
        groups.setdefault((q.base, q.quote), []).append(q)

    for (base, quote), items in groups.items():
        if len(items) < 2:
            continue
        pair_alerts: list[SpreadAlert] = []
        for lowest in items:
            for highest in items:
                if lowest.pool.address == highest.pool.address or lowest.price >= highest.price:
                    continue

                buy_price = lowest.price
                sell_price = highest.price

                # 1. 价格合理性校验 (buy_price 与 sell_price 必须大于0且 math.isfinite)
                if (
                    buy_price <= 0
                    or sell_price <= 0
                    or not math.isfinite(buy_price)
                    or not math.isfinite(sell_price)
                ):
                    logger.error(
                        "[SPREAD_ANOMALY] 价格非有限正数: buy_price=%s, sell_price=%s (%s/%s), 丢弃",
                        buy_price,
                        sell_price,
                        base,
                        quote,
                    )
                    continue

                # 价差比率 sell/buy 必须在 MIN_PLAUSIBLE_PRICE_RATIO 到 MAX_PLAUSIBLE_PRICE_RATIO 之间 (真实 DEX 不可能超 ±10%)
                price_ratio = sell_price / buy_price
                if not math.isfinite(price_ratio) or not (
                    MIN_PLAUSIBLE_PRICE_RATIO <= price_ratio <= MAX_PLAUSIBLE_PRICE_RATIO
                ):
                    logger.error(
                        "[SPREAD_ANOMALY] 价差比率异常: buy_price=%.6f, sell_price=%.6f, "
                        "ratio=%.4f (允许范围: %.2f~%.2f) (%s/%s), 丢弃",
                        buy_price,
                        sell_price,
                        price_ratio,
                        MIN_PLAUSIBLE_PRICE_RATIO,
                        MAX_PLAUSIBLE_PRICE_RATIO,
                        base,
                        quote,
                    )
                    continue

                gross_pct = (sell_price - buy_price) / buy_price * 100.0
                fee_pct = (lowest.pool.fee_bps + highest.pool.fee_bps) / 100.0  # bps -> %
                net_pct = gross_pct - fee_pct - slippage_buffer_pct

                # gross_spread_pct 与 net_spread_pct 绝对值必须在 MAX_PLAUSIBLE_SPREAD_PCT 以内
                if (
                    not math.isfinite(gross_pct)
                    or not math.isfinite(net_pct)
                    or not (-MAX_PLAUSIBLE_SPREAD_PCT <= gross_pct <= MAX_PLAUSIBLE_SPREAD_PCT)
                    or not (-MAX_PLAUSIBLE_SPREAD_PCT <= net_pct <= MAX_PLAUSIBLE_SPREAD_PCT)
                ):
                    logger.error(
                        "[SPREAD_ANOMALY] 价差百分比异常: gross=%.2f%%, net=%.2f%% "
                        "(合理范围: -%.1f%%~%.1f%%) (%s/%s), 丢弃",
                        gross_pct,
                        net_pct,
                        MAX_PLAUSIBLE_SPREAD_PCT,
                        MAX_PLAUSIBLE_SPREAD_PCT,
                        base,
                        quote,
                    )
                    continue

                if net_pct <= 0:
                    continue

                # 计算最优套利金额与利润容量
                tvl_buy, tvl_sell = lowest.pool.tvl_usd, highest.pool.tvl_usd
                known_tvl = all(math.isfinite(value) and value > 0 for value in (tvl_buy, tvl_sell))
                bottleneck_tvl = min(tvl_buy, tvl_sell) if known_tvl else 0.0

                profit_500, size_500, max_cap = estimate_realized_profit_usd(
                    gross_pct=gross_pct,
                    fee_pct=fee_pct,
                    tvl_buy_usd=tvl_buy,
                    tvl_sell_usd=tvl_sell,
                    size_usd=500.0,
                )
                opt_size = min(500.0, max_cap / 2.0) if max_cap > 0 else 0.0
                max_profit, _, _ = estimate_realized_profit_usd(
                    gross_pct=gross_pct,
                    fee_pct=fee_pct,
                    tvl_buy_usd=tvl_buy,
                    tvl_sell_usd=tvl_sell,
                    size_usd=opt_size,
                )

                # 2. 利润上限保护 (max_profit_usd > 1000.0 时记 PROFIT_ANOMALY 并丢弃)
                if (
                    max_profit > 1000.0
                    or profit_500 > 1000.0
                    or not math.isfinite(max_profit)
                    or not math.isfinite(profit_500)
                ):
                    logger.error(
                        "[PROFIT_ANOMALY] 跨池套利利润计算异常: max_profit_usd=%.2f > 1000.0 "
                        "(profit_at_500u=%.2f, %s/%s), 丢弃该 alert",
                        max_profit,
                        profit_500,
                        base,
                        quote,
                    )
                    continue

                pair_alerts.append(
                    SpreadAlert(
                        base=base,
                        quote=quote,
                        buy_pool=lowest.pool,
                        sell_pool=highest.pool,
                        buy_price=lowest.price,
                        sell_price=highest.price,
                        gross_spread_pct=gross_pct,
                        total_fee_pct=fee_pct + slippage_buffer_pct,
                        net_spread_pct=net_pct,
                        fee_pct=fee_pct,
                        bottleneck_tvl=bottleneck_tvl,
                        max_capacity_usd=max_cap,
                        optimal_size_usd=opt_size,
                        max_profit_usd=max_profit,
                        profit_at_500u=profit_500,
                        ts=lowest.ts,
                        capacity_source="MODEL_ESTIMATE" if known_tvl else "UNKNOWN",
                    )
                )

        if pair_alerts:
            alerts.append(max(pair_alerts, key=lambda a: (a.max_profit_usd, a.net_spread_pct)))

    return sorted(alerts, key=lambda a: a.net_spread_pct, reverse=True)

def scan_once(
    pools: list[AnyPool],
    reader: PoolReader | None = None,
    slippage_buffer_pct: float = 0.2,
) -> tuple[list[PriceQuote], list[SpreadAlert]]:
    """对给定池子做一轮扫描, 返回 (报价列表, 套利机会列表)."""
    if reader is None:
        raise ValueError("An explicitly injected read-only reader is required")
    rd = reader
    clean_pools = [
        p
        for p in pools
        if not (
            is_tax_token(getattr(p, "token0", ""))
            or is_tax_token(getattr(p, "token1", ""))
            or is_tax_token(getattr(p, "currency0", ""))
            or is_tax_token(getattr(p, "currency1", ""))
            or is_tax_token(getattr(p, "address", ""))
        )
    ]
    if hasattr(rd, "batch_quote"):
        quotes = rd.batch_quote(clean_pools)
    else:
        quotes = []
        for p in clean_pools:
            try:
                quotes.append(rd.quote(p))
            except (HttpHalt, PoolHalt):
                raise
            except Exception as exc:  # noqa: BLE001 - 只读监控, 单池失败不应中断全局
                print(f"  ⚠️ 读取失败 {getattr(p, 'label', str(p))}: {type(exc).__name__}: {exc}")
                continue

    clean_quotes = [
        q
        for q in quotes
        if not (
            is_tax_token(q.base)
            or is_tax_token(q.quote)
            or is_tax_token(getattr(q.pool, "token0", ""))
            or is_tax_token(getattr(q.pool, "token1", ""))
            or is_tax_token(getattr(q.pool, "currency0", ""))
            or is_tax_token(getattr(q.pool, "currency1", ""))
        )
    ]
    return clean_quotes, find_spreads(clean_quotes, slippage_buffer_pct=slippage_buffer_pct)
