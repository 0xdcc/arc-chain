"""Historical indicative capacity estimate, not realized or executable profit.

No Gas cost is inferred or deducted. Zero triples mean no valid estimate, not
verified zero transaction costs. This module never authorizes an execution.
"""
from __future__ import annotations

import math
import time
from decimal import Decimal
from fractions import Fraction

Numeric = int | float | Decimal


def _number(value: Numeric) -> Fraction | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
    elif isinstance(value, float) and not math.isfinite(value):
        return None
    return Fraction(Decimal(str(value)))


def _result(profit: Fraction, size: Fraction, capacity: Fraction) -> tuple[float, float, float]:
    # Compatibility boundary only; every preceding calculation is rational.
    try:
        converted = (float(profit), float(size), float(capacity))
    except OverflowError:
        return (0.0, 0.0, 0.0)
    if not all(math.isfinite(value) for value in converted):
        return (0.0, 0.0, 0.0)
    return converted


def estimate_realized_profit_usd(
    gross_pct: Numeric,
    fee_pct: Numeric,
    tvl_buy_usd: Numeric,
    tvl_sell_usd: Numeric,
    size_usd: Numeric,
    *,
    quoted_profit_at_500u: Numeric = 0,
    quoted_capacity_usd: Numeric = 0,
    quote_ts: Numeric = 0,
    now: Numeric | None = None,
) -> tuple[float, float, float]:
    """Return historical indicative (profit, size, capacity), never settled PnL.

    Missing TVL may reuse a fresh measured 500 USD quote, capped by current
    edge and measured size/capacity. TVL-based estimates retain the historical
    quadratic model. Neither path proves executable liquidity or net-of-Gas profit.
    """
    gross, fee, size = (_number(value) for value in (gross_pct, fee_pct, size_usd))
    empty = (0.0, 0.0, 0.0)
    if gross is None or fee is None or size is None or fee < 0:
        return empty
    buy, sell = (_number(value) for value in (tvl_buy_usd, tvl_sell_usd))
    edge = max(Fraction(0), (gross - fee) / 100)
    if buy is None or sell is None or buy <= 0 or sell <= 0:
        quoted, capacity, timestamp = (
            _number(value) for value in (quoted_profit_at_500u, quoted_capacity_usd, quote_ts)
        )
        if quoted is None or capacity is None or timestamp is None:
            return empty
        if quoted <= 0 or capacity <= 0 or timestamp <= 0 or size <= 0:
            return empty
        observed_now = _number(time.time() if now is None else now)
        if observed_now is None or not 0 <= observed_now - timestamp <= 10:
            return empty
        actual_size = min(size, capacity, Fraction(500))
        return _result(actual_size * min(edge, quoted / 500), actual_size, capacity)
    if edge <= 0:
        return empty
    decay = 2 / buy + 2 / sell
    max_capacity = edge / decay
    actual_size = max(Fraction(0), min(size, max_capacity))
    profit = actual_size * (edge - decay * actual_size / 2)
    return _result(profit, actual_size, max_capacity)
