"""Bounded integer CLMM swaps; incomplete tick evidence never authorizes crossing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from arbitrage_contracts.arc_extensions import TickCoverage
from arbitrage_contracts.quote import QuoteStatus
from state_graph.clmm_math import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    compute_swap_step,
    get_sqrt_ratio_at_tick,
)
from state_graph.evaluate import single_segment_target
from state_graph.types import PoolStateSnapshot


class MultiTickError(ValueError):
    """Invalid or unbound liquidity evidence."""


@dataclass(frozen=True, slots=True)
class PoolTickTable:
    """A copied, immutable table; route consumers additionally require block_hash."""

    pool_id: str
    coverage: TickCoverage
    tick_spacing: int
    initialized_ticks: Mapping[int, int]
    block_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "initialized_ticks", MappingProxyType(dict(self.initialized_ticks))
        )

    def get_next_initialized_tick(self, current_tick: int, zero_for_one: bool) -> int | None:
        """Return the nearest initialized tick in the requested direction."""
        candidates = [
            t
            for t in self.initialized_ticks
            if (t <= current_tick if zero_for_one else t > current_tick)
        ]
        return (max(candidates) if zero_for_one else min(candidates)) if candidates else None


@dataclass(frozen=True, slots=True)
class MultiTickHopResult:
    amount_in_consumed: int
    amount_out_produced: int
    total_fee_amount: int
    final_sqrt_price_x96: int
    final_tick: int
    final_liquidity: int
    ticks_crossed: int
    status: QuoteStatus
    error: str | None = None


def _tick_at_price(price: int) -> int:
    """Integer inverse TickMath; no floating-point logarithms."""
    lo, hi = MIN_TICK, MAX_TICK
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if get_sqrt_ratio_at_tick(mid) <= price:
            lo = mid
        else:
            hi = mid - 1
    return lo


def execute_multi_tick_hop(
    snapshot: PoolStateSnapshot,
    amount_in: int,
    zero_for_one: bool,
    tick_table: PoolTickTable | None = None,
    max_crossings: int = 16,
) -> MultiTickHopResult:
    """Quote within proven coverage; partial consumption is never QUOTED."""
    price, tick, liquidity = snapshot.sqrt_price_x96, snapshot.tick, snapshot.liquidity
    remaining, total_out, total_fee, crossings = amount_in, 0, 0, 0

    def result(error: str | None = None) -> MultiTickHopResult:
        return MultiTickHopResult(
            amount_in - remaining,
            total_out,
            total_fee,
            price,
            tick,
            liquidity,
            crossings,
            QuoteStatus.UNSUPPORTED if error else QuoteStatus.QUOTED,
            error,
        )

    fields = (
        amount_in,
        max_crossings,
        price,
        tick,
        liquidity,
        snapshot.fee_pips,
        snapshot.tick_spacing,
    )
    if any(type(v) is not int for v in fields) or type(zero_for_one) is not bool:
        # Do not perform arithmetic on malformed caller input.
        return MultiTickHopResult(
            0,
            0,
            0,
            price,
            tick,
            liquidity,
            0,
            QuoteStatus.UNSUPPORTED,
            "CLMM inputs must be exact integers",
        )
    if (
        amount_in <= 0
        or max_crossings < 0
        or not MIN_TICK <= tick < MAX_TICK
        or not MIN_SQRT_RATIO < price < MAX_SQRT_RATIO
        or not 0 < liquidity < 2**128
        or not 0 <= snapshot.fee_pips < 1_000_000
        or not 0 < snapshot.tick_spacing < 2**23
    ):
        return result("Invalid CLMM bounds, amount, fee or spacing")
    if not get_sqrt_ratio_at_tick(tick) <= price <= get_sqrt_ratio_at_tick(tick + 1):
        return result("Inconsistent tick and sqrt price")

    if tick_table is not None:
        cov = tick_table.coverage
        if cov.is_complete is not True:
            return result("Incomplete tick coverage cannot authorize crossing")
        if (
            tick_table.pool_id != snapshot.pool_id
            or cov.pool_id != snapshot.pool_id
            or cov.as_of_block != snapshot.block_number
            or cov.current_tick != snapshot.tick
            or tick_table.tick_spacing != snapshot.tick_spacing
            or (
                tick_table.block_hash is not None
                and tick_table.block_hash.lower() != snapshot.block_hash.lower()
            )
        ):
            return result("Tick table identity/state mismatch")
        if (
            type(cov.initialized_ticks_count) is not int
            or cov.initialized_ticks_count != len(tick_table.initialized_ticks)
            or not MIN_TICK <= cov.min_tick <= tick <= cov.max_tick <= MAX_TICK
        ):
            return result("Invalid tick coverage count or bounds")
        for initialized, net in tick_table.initialized_ticks.items():
            if (
                type(initialized) is not int
                or type(net) is not int
                or initialized % snapshot.tick_spacing != 0
                or not cov.min_tick <= initialized <= cov.max_tick
                or not -(2**127) <= net < 2**127
            ):
                return result("Invalid initialized tick or liquidityNet")

    while remaining > 0:
        if not 0 < liquidity < 2**128:
            return result("Zero or invalid active liquidity encountered")
        initialized = False
        if tick_table is None:
            try:
                target = single_segment_target(
                    snapshot, "zero_for_one" if zero_for_one else "one_for_zero"
                )
            except ValueError as exc:
                return result(f"Unknown single-tick boundary: {exc}")
            next_tick = None
        else:
            cov = tick_table.coverage
            next_tick = tick_table.get_next_initialized_tick(tick, zero_for_one)
            initialized = next_tick is not None
            if next_tick is None:
                next_tick = cov.min_tick if zero_for_one else cov.max_tick
            target = get_sqrt_ratio_at_tick(next_tick)
            target = max(MIN_SQRT_RATIO + 1, min(MAX_SQRT_RATIO - 1, target))
            if target > price if zero_for_one else target < price:
                return result("Swap exceeded verified tick coverage")
            if target == price and not initialized:
                return result("Swap exceeded verified tick coverage")
        step = compute_swap_step(price, target, liquidity, remaining, snapshot.fee_pips)
        consumed = step.amount_in + step.fee_amount
        if consumed < 0 or consumed > remaining:
            return result("Invalid swap input consumption")
        previous = price
        remaining -= consumed
        total_out += step.amount_out
        total_fee += step.fee_amount
        price = step.next_sqrt_price_x96
        if price == target:
            if tick_table is None:
                # At an unproven segment edge preserve upstream F01 fail-closed behavior.
                return result("Swap crossed single-tick boundary; multi_tick coverage not active")
            if not initialized:
                if remaining:
                    return result("Swap exceeded verified tick coverage")
                tick = _tick_at_price(price)
                break
            if crossings >= max_crossings:
                return result(f"Exceeded max tick crossings limit ({max_crossings})")
            assert next_tick is not None
            # Never default missing liquidityNet to zero.
            net = tick_table.initialized_ticks[next_tick]
            liquidity += -net if zero_for_one else net
            if not 0 <= liquidity < 2**128:
                return result("Liquidity overflow/underflow at tick crossing")
            crossings += 1
            tick = next_tick - 1 if zero_for_one else next_tick
        else:
            tick = _tick_at_price(price)
            if remaining:
                return result("Partial input consumption without a proven crossing")
        if consumed == 0 and price == previous and not initialized:
            return result("Swap made no progress")
    if total_out <= 0 or remaining:
        return result("Zero output or unconsumed input")
    return result()
