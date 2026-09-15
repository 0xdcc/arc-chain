"""Models and data structures for independent offline token graph and triangular path contracts.

Financial Semantics & Mathematical Invariants:
- Multiplier Formula:
    R = (1 - f1) * (1 - f2) * (1 - f3) * P1 * P2 * P3
    gross_multiplier = P1 * P2 * P3
    fee_multiplier = (1 - f1) * (1 - f2) * (1 - f3)
    expected_multiplier = gross_multiplier * fee_multiplier
- Rate Spread vs. Realized Net Profit:
    `net_profit_pct` represents the fee-adjusted and slippage-buffer-adjusted geometric rate spread percentage:
        net_profit_pct = (expected_multiplier - 1.0) * 100.0 - slippage_buffer_pct
    FINANCIAL NOTICE: This metric reflects exchange rate expansion net of pool swap fees
    and parameterized slippage buffer. It is NOT finalized on-chain net profit because
    transaction gas fees, priority fees, and block execution variance are UNKNOWN and NOT deducted.
    Calling code must not treat this indicator as realized on-chain profit or execution guarantee.
- Integer & Scale Boundaries:
    Amounts and capacities (optimal_size_usd, bottleneck_tvl) derived here use a continuous
    Cournot-style TVL approximation solely for research screening and sizing estimation.
    Physical transaction dispatch requires discrete uint256 atom arithmetic and explicit
    output floor boundaries (output_floor >= amount_in + ceil(gas_atoms) + 1 atom) in planning/execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from research.market_data.multicall import AnyPool

ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"


@dataclass
class SwapLeg:
    """Individual execution leg within a 3-hop triangular path.

    Attributes:
        from_token: Input token address or normalized symbol.
        to_token: Output token address or normalized symbol.
        pool: Liquidity pool specification (V3 PoolSpec or V4 V4PoolSpec).
        rate: Spot exchange rate: units of to_token yielded per 1 unit of from_token (pre-fee).
        fee_bps: Pool swap fee in basis points (e.g. 30.0 for 0.3%).
        effective_rate: Rate net of pool swap fee: rate * (1.0 - fee_bps / 10000.0).
        from_symbol: Human-readable symbol of the input token.
        to_symbol: Human-readable symbol of the output token.
    """

    from_token: str
    to_token: str
    pool: AnyPool
    rate: float
    fee_bps: float
    effective_rate: float
    from_symbol: str = ""
    to_symbol: str = ""


@dataclass(frozen=True)
class DirectedEdge:
    """Directed edge in token graph representing a single-pool exchange capability.

    Attributes:
        from_token: Source token identifier.
        to_token: Destination token identifier.
        pool: Underlying liquidity pool.
        rate: Spot exchange rate (1 from_token -> rate to_token before fee).
        fee_bps: Pool fee in basis points.
        effective_rate: Effective exchange rate after pool fee.
        weight: Negative log of effective rate: -ln(effective_rate), for shortest path / negative cycle search.
        from_symbol: Symbol of source token.
        to_symbol: Symbol of destination token.
        block_number: Height of the block at which the quote was sampled.
    """

    from_token: str
    to_token: str
    pool: AnyPool
    rate: float
    fee_bps: float
    effective_rate: float
    weight: float
    from_symbol: str = ""
    to_symbol: str = ""
    block_number: int = 0


@dataclass
class TriangularArbAlert:
    """Alert record representing an identified 3-hop triangular arbitrage opportunity.

    Unified Topology Convention:
        `cycle` is a 4-tuple of token nodes: (A, B, C, A), where length == hops + 1
        and cycle[0].lower() == cycle[-1].lower().
        `legs` contains 3 distinct SwapLeg instances corresponding to A->B, B->C, C->A.
    """

    start_token: str
    cycle: tuple[str, ...]
    legs: list[SwapLeg]
    gross_multiplier: float
    fee_multiplier: float
    expected_multiplier: float
    slippage_buffer_pct: float
    net_multiplier: float
    gross_profit_pct: float
    net_profit_pct: float
    total_fee_pct: float
    bottleneck_tvl: float = 0.0
    max_capacity_usd: float = 0.0
    optimal_size_usd: float = 0.0
    max_profit_usd: float = 0.0
    profit_at_500u: float = 0.0
    base_token: str | None = None
    block_skew: int = 0
    block_number: int = 0
    ts: float = field(default_factory=time.time)

    def __str__(self) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.ts))
        cycle_str = " → ".join(self.cycle)
        cap_str = (
            f" | 容量: 最优 ${self.optimal_size_usd:,.0f}U "
            f"(500U净赚 +${self.profit_at_500u:.2f}, 顶峰 +${self.max_profit_usd:.2f})"
            if self.optimal_size_usd > 0
            else ""
        )
        block_str = f" | Block #{self.block_number} (skew={self.block_skew})"
        header = (
            f"[{t}] 🔺 三角套利: {cycle_str} | "
            f"毛利 {self.gross_profit_pct:.3f}% - 费用 {self.total_fee_pct:.3f}% - "
            f"滑点 {self.slippage_buffer_pct:.3f}% = 净利 {self.net_profit_pct:.3f}%"
            f"{cap_str}{block_str}"
        )
        leg_lines = []
        for idx, leg in enumerate(self.legs, 1):
            f_sym = leg.from_symbol or leg.from_token[:8]
            t_sym = leg.to_symbol or leg.to_token[:8]
            leg_lines.append(
                f"    Leg {idx}: {f_sym} → {t_sym} "
                f"via {leg.pool.label} (费率 {leg.fee_bps / 100.0:.3f}%) @{leg.rate:.6g}"
            )
        return "\n".join([header, *leg_lines])
