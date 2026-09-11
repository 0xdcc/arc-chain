"""Arc Wall Premium Scenario Analysis and External Settlement Accounting (T32)

Enforces:
- Separation of on-chain liquidity profit from external OTC wall spread
- Zero-premium stress testing (premium collapse to parity must be survivable)
- Incomplete delivery guard: unsettled OTC trades cannot be counted as realized profit
- Double-lock trap prevention: Concurrent order dispatch does NOT constitute locked profit
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arc_research.wall.normalization import NormalizedWallQuote


class PremiumAnalysisError(ValueError):
    """Raised when premium analysis or settlement accounting invariants fail."""


@dataclass(frozen=True, slots=True)
class PremiumStressResult:
    """Stress test outcome under premium compression."""

    base_spread_bps: int
    zero_premium_net_atoms: int
    survives_zero_premium: bool
    recommended_min_onchain_spread_bps: int


@dataclass(frozen=True, slots=True)
class CrossMarketSettlementReport:
    """Audit report on cross-market OTC vs DEX execution."""

    pair_id: str
    dex_trade_settled: bool
    otc_delivery_settled: bool
    is_realized_profit: bool
    realized_net_atoms: int | None
    unsettled_notional_atoms: int
    audit_verdict: str


class WallPremiumAnalyzer:
    """Analyzes spreads, premium decay scenarios, and cross-market settlement."""

    @staticmethod
    def evaluate_spread(
        onchain_effective_price: Decimal,
        otc_exit_rate: Decimal,
    ) -> int:
        """Compute spread in basis points between on-chain execution and OTC exit."""
        if otc_exit_rate <= 0:
            raise PremiumAnalysisError(f"otc_exit_rate must be positive, got {otc_exit_rate}")

        spread = (onchain_effective_price - otc_exit_rate) / otc_exit_rate
        return int(spread * Decimal(10000))

    @staticmethod
    def stress_test_zero_premium(
        onchain_gross_out_atoms: int,
        amount_in_atoms: int,
        otc_all_in_cost_atoms: int,
        gas_cost_atoms: int,
    ) -> PremiumStressResult:
        """Evaluate sensitivity under the zero-premium scenario (parity 1.0000).

        Invariants:
        1. If on-chain price collapses to exact parity (1 USDC in = 1 USDC out),
           the net profit becomes: amount_in - otc_all_in_cost - gas_cost.
        2. If this value is <= 0, the opportunity depends entirely on on-chain premium.
        """
        zero_prem_net = amount_in_atoms - otc_all_in_cost_atoms - gas_cost_atoms
        survives = zero_prem_net > 0

        # Baseline spread
        effective_onchain = Decimal(onchain_gross_out_atoms) / Decimal(amount_in_atoms)
        effective_otc = Decimal(otc_all_in_cost_atoms) / Decimal(amount_in_atoms)
        base_spread = int(((effective_onchain - effective_otc) / effective_otc) * Decimal(10000))

        # Required buffer to break even without premium
        break_even_bps = max(0, int(((Decimal(otc_all_in_cost_atoms + gas_cost_atoms) / Decimal(amount_in_atoms)) - 1) * Decimal(10000)))

        return PremiumStressResult(
            base_spread_bps=base_spread,
            zero_premium_net_atoms=zero_prem_net,
            survives_zero_premium=survives,
            recommended_min_onchain_spread_bps=break_even_bps + 25,  # 25 bps safety margin
        )

    @staticmethod
    def verify_settlement_status(
        pair_id: str,
        dex_trade_settled: bool,
        otc_delivery_settled: bool,
        claimed_profit_atoms: int,
        amount_in_atoms: int,
    ) -> CrossMarketSettlementReport:
        """Verify cross-market delivery.

        CRITICAL INVARIANT:
        Concurrent order dispatch does NOT equal locked profit!
        If either DEX or OTC trade is pending delivery, realized profit MUST be None.
        """
        if not dex_trade_settled or not otc_delivery_settled:
            verdict = "DELIVERY_PENDING: Unsettled legs cannot be recognized as realized profit"
            return CrossMarketSettlementReport(
                pair_id=pair_id,
                dex_trade_settled=dex_trade_settled,
                otc_delivery_settled=otc_delivery_settled,
                is_realized_profit=False,
                realized_net_atoms=None,
                unsettled_notional_atoms=amount_in_atoms,
                audit_verdict=verdict,
            )

        return CrossMarketSettlementReport(
            pair_id=pair_id,
            dex_trade_settled=True,
            otc_delivery_settled=True,
            is_realized_profit=True,
            realized_net_atoms=claimed_profit_atoms,
            unsettled_notional_atoms=0,
            audit_verdict="SETTLED: Bilateral cross-market execution verified",
        )
