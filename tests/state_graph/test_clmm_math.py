"""Comprehensive discrete integer math verification for CLMM."""

from __future__ import annotations

import pytest

from state_graph.clmm_math import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    Q96,
    ceil_div,
    compress_tick,
    compute_swap_step,
    get_amount0_delta,
    get_amount1_delta,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
)


def test_ceil_div() -> None:
    assert ceil_div(0, 5) == 0
    assert ceil_div(5, 5) == 1
    assert ceil_div(6, 5) == 2
    assert ceil_div(9, 5) == 2
    assert ceil_div(10, 5) == 2
    with pytest.raises(ValueError, match="positive"):
        ceil_div(5, 0)
    with pytest.raises(ValueError, match="positive"):
        ceil_div(5, -1)


def test_tick_and_sqrt_price_boundaries() -> None:
    assert get_sqrt_ratio_at_tick(MIN_TICK) == MIN_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(MAX_TICK) == MAX_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(0) == Q96

    with pytest.raises(ValueError, match="bounds"):
        get_sqrt_ratio_at_tick(MIN_TICK - 1)
    with pytest.raises(ValueError, match="bounds"):
        get_sqrt_ratio_at_tick(MAX_TICK + 1)


@pytest.mark.parametrize(
    "tick",
    [
        MIN_TICK,
        -500000,
        -100000,
        -1,
        0,
        1,
        100000,
        500000,
        MAX_TICK - 1,
    ],
)
def test_tick_sqrt_ratio_roundtrip(tick: int) -> None:
    sqrt_price = get_sqrt_ratio_at_tick(tick)
    recovered_tick = get_tick_at_sqrt_ratio(sqrt_price)
    assert recovered_tick == tick


def test_get_tick_at_sqrt_ratio_bounds() -> None:
    assert get_tick_at_sqrt_ratio(MIN_SQRT_RATIO) == MIN_TICK
    with pytest.raises(ValueError, match="out of bounds"):
        get_tick_at_sqrt_ratio(MIN_SQRT_RATIO - 1)
    with pytest.raises(ValueError, match="out of bounds"):
        get_tick_at_sqrt_ratio(MAX_SQRT_RATIO + 1)


@pytest.mark.parametrize(
    ("tick", "spacing", "expected"),
    [
        (0, 10, 0),
        (5, 10, 0),
        (10, 10, 1),
        (15, 10, 1),
        (-1, 10, -1),
        (-5, 10, -1),
        (-10, 10, -1),
        (-11, 10, -2),
        (-60, 60, -1),
        (-61, 60, -2),
    ],
)
def test_compress_tick(tick: int, spacing: int, expected: int) -> None:
    # Must floor toward negative infinity (//), never truncate towards zero (int(/))
    assert compress_tick(tick, spacing) == expected


def test_compress_tick_invalid_spacing() -> None:
    with pytest.raises(ValueError, match="tick_spacing must be positive"):
        compress_tick(10, 0)
    with pytest.raises(ValueError, match="tick_spacing must be positive"):
        compress_tick(10, -5)


def test_get_amount0_delta() -> None:
    sqrt_a = get_sqrt_ratio_at_tick(0)  # 1.0 * 2^96
    sqrt_b = get_sqrt_ratio_at_tick(100)  # > 1.0 * 2^96
    liquidity = 10**18

    # delta = L * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
    amount0_down = get_amount0_delta(sqrt_a, sqrt_b, liquidity, round_up=False)
    amount0_up = get_amount0_delta(sqrt_a, sqrt_b, liquidity, round_up=True)
    assert amount0_down > 0
    assert amount0_up >= amount0_down
    assert amount0_up - amount0_down <= 1

    # Strict non-zero rounding up verification
    sqrt_1 = get_sqrt_ratio_at_tick(1)
    up_strict = get_amount0_delta(sqrt_a, sqrt_1, liquidity + 7, round_up=True)
    down_strict = get_amount0_delta(sqrt_a, sqrt_1, liquidity + 7, round_up=False)
    assert up_strict == down_strict + 1

    # zero liquidity
    assert get_amount0_delta(sqrt_a, sqrt_b, 0, round_up=True) == 0
    # identical prices
    assert get_amount0_delta(sqrt_a, sqrt_a, liquidity, round_up=True) == 0


def test_get_amount1_delta() -> None:
    sqrt_a = get_sqrt_ratio_at_tick(0)
    sqrt_b = get_sqrt_ratio_at_tick(100)
    liquidity = 10**18

    amount1_down = get_amount1_delta(sqrt_a, sqrt_b, liquidity, round_up=False)
    amount1_up = get_amount1_delta(sqrt_a, sqrt_b, liquidity, round_up=True)
    assert amount1_down > 0
    assert amount1_up >= amount1_down
    assert amount1_up - amount1_down <= 1

    # Strict non-zero rounding up verification
    sqrt_1 = get_sqrt_ratio_at_tick(1)
    up_strict1 = get_amount1_delta(sqrt_a, sqrt_1, liquidity + 7, round_up=True)
    down_strict1 = get_amount1_delta(sqrt_a, sqrt_1, liquidity + 7, round_up=False)
    assert up_strict1 == down_strict1 + 1

    assert get_amount1_delta(sqrt_a, sqrt_b, 0, round_up=True) == 0
    assert get_amount1_delta(sqrt_a, sqrt_a, liquidity, round_up=True) == 0


def test_compute_swap_step_zero_for_one_reaches_target() -> None:
    # zero_for_one: price drops (sqrt_cur >= sqrt_target)
    sqrt_cur = get_sqrt_ratio_at_tick(100)
    sqrt_target = get_sqrt_ratio_at_tick(0)
    liquidity = 10**18
    fee_pips = 500  # 0.05%

    # Provide large amount so target is reached
    large_amount = 10**22
    step = compute_swap_step(sqrt_cur, sqrt_target, liquidity, large_amount, fee_pips)

    assert step.next_sqrt_price_x96 == sqrt_target
    assert step.amount_in > 0
    assert step.amount_out > 0
    assert step.fee_amount > 0
    assert step.amount_in + step.fee_amount <= large_amount


def test_compute_swap_step_zero_for_one_partial() -> None:
    sqrt_cur = get_sqrt_ratio_at_tick(100)
    sqrt_target = get_sqrt_ratio_at_tick(0)
    liquidity = 10**18
    fee_pips = 500

    small_amount = 1000  # very small, won't reach target
    step = compute_swap_step(sqrt_cur, sqrt_target, liquidity, small_amount, fee_pips)

    assert step.next_sqrt_price_x96 < sqrt_cur
    assert step.next_sqrt_price_x96 > sqrt_target
    assert step.amount_in + step.fee_amount == small_amount
    assert step.amount_out > 0


def test_compute_swap_step_one_for_zero() -> None:
    # one_for_zero: price rises (sqrt_cur <= sqrt_target)
    sqrt_cur = get_sqrt_ratio_at_tick(0)
    sqrt_target = get_sqrt_ratio_at_tick(100)
    liquidity = 10**18
    fee_pips = 3000  # 0.3%

    large_amount = 10**22
    step = compute_swap_step(sqrt_cur, sqrt_target, liquidity, large_amount, fee_pips)

    assert step.next_sqrt_price_x96 == sqrt_target
    assert step.amount_in > 0
    assert step.amount_out > 0
    assert step.fee_amount > 0


def test_compute_swap_step_zero_remaining_or_liquidity() -> None:
    sqrt_cur = get_sqrt_ratio_at_tick(0)
    sqrt_target = get_sqrt_ratio_at_tick(100)

    # 0 amount remaining
    step0 = compute_swap_step(sqrt_cur, sqrt_target, 10**18, 0, 500)
    assert step0.amount_in == 0
    assert step0.amount_out == 0

    # 0 liquidity
    step_liq = compute_swap_step(sqrt_cur, sqrt_target, 0, 1000, 500)
    assert step_liq.amount_in == 0
    assert step_liq.amount_out == 0
