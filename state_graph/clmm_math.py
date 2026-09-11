"""Discrete integer mathematics for Concentrated Liquidity Market Makers (CLMM).

Strictly implements Uniswap V3 integer arithmetic and Q64.96 fixed-point math
with no floating-point calculations to eliminate precision drift and nondeterminism.
"""

from __future__ import annotations

from state_graph.types import SwapStepResult

Q96 = 1 << 96
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
FEE_DENOMINATOR = 1000000


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer division rounding up."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return (numerator + denominator - 1) // denominator


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """Calculates sqrt(1.0001^tick) * 2^96 using official Uniswap V3 bit lookup."""
    abs_tick = abs(tick)
    if abs_tick > MAX_TICK:
        raise ValueError(f"tick {tick} exceeds tick bounds")

    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if (abs_tick & 0x1 != 0) else (1 << 128)
    if abs_tick & 0x2 != 0:
        ratio = (ratio * 0xFFF97272373D413259A46990580E213A) >> 128
    if abs_tick & 0x4 != 0:
        ratio = (ratio * 0xFFF2E50F5F656932EF12357CF3C7FDCC) >> 128
    if abs_tick & 0x8 != 0:
        ratio = (ratio * 0xFFE5CACA7E10E4E61C3624EAA0941CD0) >> 128
    if abs_tick & 0x10 != 0:
        ratio = (ratio * 0xFFCB9843D60F6159C9DB58835C926644) >> 128
    if abs_tick & 0x20 != 0:
        ratio = (ratio * 0xFF973B41FA98C081472E6896DFB254C0) >> 128
    if abs_tick & 0x40 != 0:
        ratio = (ratio * 0xFF2EA16466C96A3843EC78B326B52861) >> 128
    if abs_tick & 0x80 != 0:
        ratio = (ratio * 0xFE5DEE046A99A2A811C461F1969C3053) >> 128
    if abs_tick & 0x100 != 0:
        ratio = (ratio * 0xFCBE86C7900A88AEDCFFC83B479AA3A4) >> 128
    if abs_tick & 0x200 != 0:
        ratio = (ratio * 0xF987A7253AC413176F2B074CF7815E54) >> 128
    if abs_tick & 0x400 != 0:
        ratio = (ratio * 0xF3392B0822B70005940C7A398E4B70F3) >> 128
    if abs_tick & 0x800 != 0:
        ratio = (ratio * 0xE7159475A2C29B7443B29C7FA6E889D9) >> 128
    if abs_tick & 0x1000 != 0:
        ratio = (ratio * 0xD097F3BDFD2022B8845AD8F792AA5825) >> 128
    if abs_tick & 0x2000 != 0:
        ratio = (ratio * 0xA9F746462D870FDF8A65DC1F90E061E5) >> 128
    if abs_tick & 0x4000 != 0:
        ratio = (ratio * 0x70D869A156D2A1B890BB3DF62BAF32F7) >> 128
    if abs_tick & 0x8000 != 0:
        ratio = (ratio * 0x31BE135F97D08FD981231505542FCFA6) >> 128
    if abs_tick & 0x10000 != 0:
        ratio = (ratio * 0x9AA508B5B7A84E1C677DE54F3E99BC9) >> 128
    if abs_tick & 0x20000 != 0:
        ratio = (ratio * 0x5D6AF8DEDB81196699C329225EE604) >> 128
    if abs_tick & 0x40000 != 0:
        ratio = (ratio * 0x2216E584F5FA1EA926041BEDFE98) >> 128
    if abs_tick & 0x80000 != 0:
        ratio = (ratio * 0x48A170391F7DC42444E8FA2) >> 128

    if tick > 0:
        ratio = ((1 << 256) - 1) // ratio

    return (ratio >> 32) + (1 if (ratio % (1 << 32) != 0) else 0)


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """Calculates the greatest tick value such that get_sqrt_ratio_at_tick(tick) <= sqrt_price_x96.

    Uses an exact monotonic binary search (21 iterations max) over the tick domain.
    """
    if not (MIN_SQRT_RATIO <= sqrt_price_x96 <= MAX_SQRT_RATIO):
        raise ValueError(f"sqrt_price_x96 {sqrt_price_x96} out of bounds")

    low = MIN_TICK
    high = MAX_TICK
    ans = MIN_TICK
    while low <= high:
        mid = (low + high) // 2
        if get_sqrt_ratio_at_tick(mid) <= sqrt_price_x96:
            ans = mid
            low = mid + 1
        else:
            high = mid - 1
    return ans


def compress_tick(tick: int, tick_spacing: int) -> int:
    """Compresses a tick to its spacing boundary, strictly rounding towards negative infinity."""
    if tick_spacing <= 0:
        raise ValueError("tick_spacing must be positive")
    return tick // tick_spacing


def get_amount0_delta(
    sqrt_ratio_a_x96: int,
    sqrt_ratio_b_x96: int,
    liquidity: int,
    round_up: bool,
) -> int:
    """Calculates token0 delta: Δx = L * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)."""
    if sqrt_ratio_a_x96 <= 0 or sqrt_ratio_b_x96 <= 0:
        raise ValueError("sqrt_ratio must be positive")
    if liquidity < 0:
        raise ValueError("liquidity cannot be negative")
    if liquidity == 0 or sqrt_ratio_a_x96 == sqrt_ratio_b_x96:
        return 0

    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96

    numerator = (liquidity << 96) * (sqrt_ratio_b_x96 - sqrt_ratio_a_x96)
    denominator = sqrt_ratio_a_x96 * sqrt_ratio_b_x96

    return ceil_div(numerator, denominator) if round_up else numerator // denominator


def get_amount1_delta(
    sqrt_ratio_a_x96: int,
    sqrt_ratio_b_x96: int,
    liquidity: int,
    round_up: bool,
) -> int:
    """Calculates token1 delta: Δy = L * (sqrt_b - sqrt_a) / 2^96."""
    if sqrt_ratio_a_x96 <= 0 or sqrt_ratio_b_x96 <= 0:
        raise ValueError("sqrt_ratio must be positive")
    if liquidity < 0:
        raise ValueError("liquidity cannot be negative")
    if liquidity == 0 or sqrt_ratio_a_x96 == sqrt_ratio_b_x96:
        return 0

    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96

    numerator = liquidity * (sqrt_ratio_b_x96 - sqrt_ratio_a_x96)

    return ceil_div(numerator, Q96) if round_up else numerator // Q96


def get_next_sqrt_price_from_input(
    sqrt_p_x96: int,
    liquidity: int,
    amount_in: int,
    zero_for_one: bool,
) -> int:
    """Calculates next sqrt price given an input amount and swap direction."""
    if sqrt_p_x96 <= 0:
        raise ValueError("sqrt_p_x96 must be positive")
    if liquidity <= 0:
        raise ValueError("liquidity must be positive")
    if amount_in == 0:
        return sqrt_p_x96

    if zero_for_one:
        # token0 in -> price decreases: next_p = L * sqrt_p / (L + amount_in * sqrt_p)
        numerator = liquidity << 96
        product = amount_in * sqrt_p_x96
        denominator = numerator + product
        return ceil_div(numerator * sqrt_p_x96, denominator)
    else:
        # token1 in -> price increases: next_p = sqrt_p + (amount_in << 96) // L
        quotient = (amount_in << 96) // liquidity
        return sqrt_p_x96 + quotient


def compute_swap_step(
    sqrt_ratio_current_x96: int,
    sqrt_ratio_target_x96: int,
    liquidity: int,
    amount_remaining: int,
    fee_pips: int,
) -> SwapStepResult:
    """Computes a single swap step for exact-input swaps in Uniswap V3 CLMM pools."""
    if amount_remaining <= 0:
        return SwapStepResult(
            next_sqrt_price_x96=sqrt_ratio_current_x96,
            amount_in=0,
            amount_out=0,
            fee_amount=0,
        )
    if liquidity == 0:
        return SwapStepResult(
            next_sqrt_price_x96=sqrt_ratio_target_x96,
            amount_in=0,
            amount_out=0,
            fee_amount=0,
        )

    zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96

    amount_remaining_less_fee = (
        amount_remaining * (FEE_DENOMINATOR - fee_pips)
    ) // FEE_DENOMINATOR

    amount_in_to_target = (
        get_amount0_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, round_up=True)
        if zero_for_one
        else get_amount1_delta(
            sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, round_up=True
        )
    )

    if amount_remaining_less_fee >= amount_in_to_target:
        next_sqrt_ratio = sqrt_ratio_target_x96
        amount_in = amount_in_to_target
        fee_amount = ceil_div(amount_in * fee_pips, FEE_DENOMINATOR - fee_pips)
    else:
        next_sqrt_ratio = get_next_sqrt_price_from_input(
            sqrt_ratio_current_x96, liquidity, amount_remaining_less_fee, zero_for_one
        )
        amount_in = (
            get_amount0_delta(next_sqrt_ratio, sqrt_ratio_current_x96, liquidity, round_up=True)
            if zero_for_one
            else get_amount1_delta(
                sqrt_ratio_current_x96, next_sqrt_ratio, liquidity, round_up=True
            )
        )
        fee_amount = amount_remaining - amount_in

    amount_out = (
        get_amount1_delta(next_sqrt_ratio, sqrt_ratio_current_x96, liquidity, round_up=False)
        if zero_for_one
        else get_amount0_delta(sqrt_ratio_current_x96, next_sqrt_ratio, liquidity, round_up=False)
    )

    return SwapStepResult(
        next_sqrt_price_x96=next_sqrt_ratio,
        amount_in=amount_in,
        amount_out=amount_out,
        fee_amount=fee_amount,
    )
