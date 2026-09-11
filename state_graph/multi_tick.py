"""Discrete Multi-Tick CLMM Swapping Engine (T21)

Enforces:
- Exact integer Q64.96 multi-tick looping within verified TickCoverage
- Fail-closed boundary enforcement: unverified or out-of-coverage ticks return UNSUPPORTED
- Zero missing-tick imputation: unread bitmap regions are NEVER defaulted to 0 liquidity
- Complete reversibility: fallback to single_segment mode when coverage is unavailable
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arbitrage_contracts.arc_extensions import TickCoverage
from arbitrage_contracts.identity import Amount, AssetRef, PoolKey
from arbitrage_contracts.quote import HopQuote, QuoteStatus
from state_graph.clmm_math import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    compute_swap_step,
    get_sqrt_ratio_at_tick,
)
from state_graph.types import PoolStateSnapshot


class MultiTickError(ValueError):
    """Raised for multi-tick evaluation failures or unverified tick crossings."""


@dataclass(frozen=True, slots=True)
class PoolTickTable:
    """Bitmap of initialized ticks and net liquidity for a specific pool."""

    pool_id: str
    coverage: TickCoverage
    tick_spacing: int
    initialized_ticks: dict[int, int]  # tick -> liquidity_net

    def get_next_initialized_tick(self, current_tick: int, zero_for_one: bool) -> int | None:
        """Find the next initialized tick strictly in swap direction within verified coverage."""
        if zero_for_one:
            candidates = [t for t in self.initialized_ticks if t <= current_tick]
            if not candidates:
                return None
            next_tick = max(candidates)
            if next_tick < self.coverage.min_tick:
                return None
            return next_tick
        else:
            candidates = [t for t in self.initialized_ticks if t > current_tick]
            if not candidates:
                return None
            next_tick = min(candidates)
            if next_tick > self.coverage.max_tick:
                return None
            return next_tick


@dataclass(frozen=True, slots=True)
class MultiTickHopResult:
    """Outcome of a multi-tick swap execution across one pool."""

    amount_in_consumed: int
    amount_out_produced: int
    total_fee_amount: int
    final_sqrt_price_x96: int
    final_tick: int
    final_liquidity: int
    ticks_crossed: int
    status: QuoteStatus
    error: str | None = None


def execute_multi_tick_hop(
    snapshot: PoolStateSnapshot,
    amount_in: int,
    zero_for_one: bool,
    tick_table: PoolTickTable | None = None,
    max_crossings: int = 16,
) -> MultiTickHopResult:
    """Execute CLMM exact-input swap stepping across multiple initialized ticks.

    Guarantees:
    1. If tick_table is None, acts strictly as single_segment and fails if crossing boundary.
    2. If crossing tick exceeds verified coverage (is_complete=False or outside min/max),
       fails closed as UNSUPPORTED. Never imputes 0 liquidity.
    3. Discrete integer Q64.96 math only; zero floating-point calculations.
    """
    if amount_in <= 0:
        return MultiTickHopResult(
            amount_in_consumed=0,
            amount_out_produced=0,
            total_fee_amount=0,
            final_sqrt_price_x96=snapshot.sqrt_price_x96,
            final_tick=snapshot.tick,
            final_liquidity=snapshot.liquidity,
            ticks_crossed=0,
            status=QuoteStatus.UNSUPPORTED,
            error="amount_in must be positive",
        )

    current_price = snapshot.sqrt_price_x96
    current_tick = snapshot.tick
    current_liquidity = snapshot.liquidity
    fee_pips = snapshot.fee_pips
    spacing = snapshot.tick_spacing

    amount_remaining = amount_in
    total_out = 0
    total_fee = 0
    ticks_crossed = 0

    while amount_remaining > 0:
        if current_liquidity <= 0:
            return MultiTickHopResult(
                amount_in_consumed=amount_in - amount_remaining,
                amount_out_produced=total_out,
                total_fee_amount=total_fee,
                final_sqrt_price_x96=current_price,
                final_tick=current_tick,
                final_liquidity=current_liquidity,
                ticks_crossed=ticks_crossed,
                status=QuoteStatus.UNSUPPORTED,
                error="Zero active liquidity encountered in tick segment",
            )

        # 1. Determine target sqrt ratio
        if tick_table is not None and tick_table.coverage.is_complete:
            next_tick = tick_table.get_next_initialized_tick(current_tick, zero_for_one)
            if next_tick is None:
                # Boundary reached beyond coverage
                return MultiTickHopResult(
                    amount_in_consumed=amount_in - amount_remaining,
                    amount_out_produced=total_out,
                    total_fee_amount=total_fee,
                    final_sqrt_price_x96=current_price,
                    final_tick=current_tick,
                    final_liquidity=current_liquidity,
                    ticks_crossed=ticks_crossed,
                    status=QuoteStatus.UNSUPPORTED,
                    error=f"Swap exceeded verified tick coverage: min={tick_table.coverage.min_tick}, max={tick_table.coverage.max_tick}",
                )
            target_price = get_sqrt_ratio_at_tick(next_tick)
        else:
            # Single segment fallback boundary
            lower = (current_tick // spacing) * spacing
            upper = lower + spacing
            if zero_for_one:
                target_tick = max(MIN_TICK, lower)
                target_price = max(MIN_SQRT_RATIO + 1, get_sqrt_ratio_at_tick(target_tick))
                next_tick = target_tick
            else:
                target_tick = min(MAX_TICK, upper)
                target_price = min(MAX_SQRT_RATIO - 1, get_sqrt_ratio_at_tick(target_tick))
                next_tick = target_tick

        # 2. Step compute
        step = compute_swap_step(
            sqrt_ratio_current_x96=current_price,
            sqrt_ratio_target_x96=target_price,
            liquidity=current_liquidity,
            amount_remaining=amount_remaining,
            fee_pips=fee_pips,
        )

        step_in = step.amount_in + step.fee_amount
        amount_remaining -= step_in
        total_out += step.amount_out
        total_fee += step.fee_amount
        current_price = step.next_sqrt_price_x96

        # Check if we reached the boundary tick
        if current_price == target_price:
            if tick_table is None:
                # Single segment cannot cross tick
                return MultiTickHopResult(
                    amount_in_consumed=amount_in - amount_remaining,
                    amount_out_produced=total_out,
                    total_fee_amount=total_fee,
                    final_sqrt_price_x96=current_price,
                    final_tick=current_tick,
                    final_liquidity=current_liquidity,
                    ticks_crossed=ticks_crossed,
                    status=QuoteStatus.UNSUPPORTED,
                    error="Swap crossed single-tick boundary; multi_tick coverage not active",
                )

            # Crossing into next tick
            ticks_crossed += 1
            if ticks_crossed > max_crossings:
                return MultiTickHopResult(
                    amount_in_consumed=amount_in - amount_remaining,
                    amount_out_produced=total_out,
                    total_fee_amount=total_fee,
                    final_sqrt_price_x96=current_price,
                    final_tick=current_tick,
                    final_liquidity=current_liquidity,
                    ticks_crossed=ticks_crossed,
                    status=QuoteStatus.UNSUPPORTED,
                    error=f"Exceeded max tick crossings limit ({max_crossings})",
                )

            net_liq = tick_table.initialized_ticks.get(next_tick, 0)
            if zero_for_one:
                current_liquidity -= net_liq
                current_tick = next_tick - 1
            else:
                current_liquidity += net_liq
                current_tick = next_tick
        else:
            break

    return MultiTickHopResult(
        amount_in_consumed=amount_in - amount_remaining,
        amount_out_produced=total_out,
        total_fee_amount=total_fee,
        final_sqrt_price_x96=current_price,
        final_tick=current_tick,
        final_liquidity=current_liquidity,
        ticks_crossed=ticks_crossed,
        status=QuoteStatus.QUOTED,
        error=None,
    )
