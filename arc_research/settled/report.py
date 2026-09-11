"""Arc Settled Cycles Reporting and Aggregation (T34)

Aggregates reconstructed historical settled cycles:
- Separates high-confidence cycles (with full trace) from lower-confidence candidates
- Summarizes flash loan reliance and net yield distribution
- Maps out realized competitor routing patterns
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arc_research.settled.adapter import SettledCycle


@dataclass(frozen=True, slots=True)
class SettledCycleSummary:
    """Statistical and economic summary of evaluated settled cycles."""

    total_cycles_analyzed: int
    profitable_cycles_count: int
    unprofitable_cycles_count: int
    flash_loan_assisted_count: int
    full_trace_confirmed_count: int
    total_net_profit_atoms: int
    max_single_profit_atoms: int
    unique_initiators_count: int
    unique_pools_touched: tuple[str, ...]


class SettledCycleReporter:
    """Generates verifiable reports from reconstructed settled cycles."""

    @staticmethod
    def generate_summary(cycles: list[SettledCycle]) -> SettledCycleSummary:
        """Summarize a collection of reconstructed cycles."""
        if not cycles:
            return SettledCycleSummary(
                total_cycles_analyzed=0,
                profitable_cycles_count=0,
                unprofitable_cycles_count=0,
                flash_loan_assisted_count=0,
                full_trace_confirmed_count=0,
                total_net_profit_atoms=0,
                max_single_profit_atoms=0,
                unique_initiators_count=0,
                unique_pools_touched=(),
            )

        profitable = [c for c in cycles if c.is_profitable]
        unprofitable = [c for c in cycles if not c.is_profitable]
        flash_assisted = [c for c in cycles if c.flash_loan is not None]
        full_trace = [c for c in cycles if c.has_full_trace]

        total_net = sum(c.net_profit_atoms for c in cycles)
        max_profit = max(c.net_profit_atoms for c in cycles) if cycles else 0

        initiators = {c.initiator_address for c in cycles}
        pools = {hop.pool_id for c in cycles for hop in c.hops}

        return SettledCycleSummary(
            total_cycles_analyzed=len(cycles),
            profitable_cycles_count=len(profitable),
            unprofitable_cycles_count=len(unprofitable),
            flash_loan_assisted_count=len(flash_assisted),
            full_trace_confirmed_count=len(full_trace),
            total_net_profit_atoms=total_net,
            max_single_profit_atoms=max_profit,
            unique_initiators_count=len(initiators),
            unique_pools_touched=tuple(sorted(pools)),
        )
