"""Amount quote reuse never invents TVL or bypasses current edge/freshness checks."""

import time

import pytest

from arbitrage.spread_monitor import estimate_realized_profit_usd


def quoted_estimate(**overrides):
    arguments = dict(
        gross_pct=2.35,
        fee_pct=0.35,
        tvl_buy_usd=0,
        tvl_sell_usd=0,
        size_usd=30,
        quoted_profit_at_500u=22.25,
        quoted_capacity_usd=1000,
        quote_ts=time.time(),
    )
    arguments.update(overrides)
    return estimate_realized_profit_usd(**arguments)


def test_existing_amount_quote_survives_missing_tvl_without_inventing_it():
    profit, amount, capacity = quoted_estimate()
    assert profit == pytest.approx(0.6)
    assert amount == 30
    assert capacity == 1000


def test_amount_quote_is_capped_at_measured_amount_and_current_edge():
    assert quoted_estimate(size_usd=800)[1] == 500
    assert quoted_estimate(quoted_capacity_usd=20)[1] == 20
    assert quoted_estimate(gross_pct=0.1)[0] == 0
    assert quoted_estimate(quoted_profit_at_500u=1)[0] == pytest.approx(0.06)


@pytest.mark.parametrize(
    "overrides",
    [
        {"quoted_capacity_usd": 0},
        {"quoted_profit_at_500u": 0},
        {"quote_ts": time.time() - 60},
        {"quote_ts": time.time() + 60},
        {"quoted_profit_at_500u": float("nan")},
    ],
)
def test_missing_invalid_or_stale_amount_quote_is_not_executable_capacity(overrides):
    assert quoted_estimate(**overrides) == (0, 0, 0)
