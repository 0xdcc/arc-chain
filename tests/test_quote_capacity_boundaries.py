"""Offline indicative estimates; not executed profit or liquidity certification."""
from decimal import Decimal, localcontext

import pytest

from research.quote_capacity import estimate_realized_profit_usd


def estimate(**overrides):
    args = dict(gross_pct=2.35, fee_pct=0.35, tvl_buy_usd=0,
                tvl_sell_usd=0, size_usd=30, quoted_profit_at_500u=22.25,
                quoted_capacity_usd=1000, quote_ts=100, now=100)
    args.update(overrides)
    return estimate_realized_profit_usd(**args)


@pytest.mark.parametrize("now", [100, 110])
def test_fresh_boundaries(now):
    assert estimate(now=now) == pytest.approx((0.6, 30, 1000))


@pytest.mark.parametrize("now", [99, Decimal("110.00000000000000000000001")])
def test_outside_freshness_window(now):
    assert estimate(now=now) == (0, 0, 0)


@pytest.mark.parametrize("field", ["gross_pct", "fee_pct", "size_usd", "quoted_profit_at_500u", "quoted_capacity_usd", "quote_ts", "now"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_invalid_single_input(field, value):
    assert estimate(**{field: value}) == (0, 0, 0)


@pytest.mark.parametrize("field", ["tvl_buy_usd", "tvl_sell_usd"])
def test_missing_one_tvl_does_not_invent_liquidity(field):
    args = dict(tvl_buy_usd=10000, tvl_sell_usd=10000)
    args[field] = float("nan")
    assert estimate(**args) == pytest.approx((0.6, 30, 1000))


def test_quadratic_estimate_exact_reference():
    # edge=0.02, decay=0.0004, capacity=50, P(30)=0.42.
    assert estimate(tvl_buy_usd=10000, tvl_sell_usd=10000) == pytest.approx((0.42, 30, 50))


@pytest.mark.parametrize("precision", [1, 2, 5, 28])
def test_context_independent_intermediate_arithmetic(precision):
    with localcontext() as ctx:
        ctx.prec = precision
        assert estimate(tvl_buy_usd=10000, tvl_sell_usd=10000) == pytest.approx((0.42, 30, 50))
        assert estimate(now=Decimal("110.00000000000000000000001")) == (0, 0, 0)
