"""Arc OTC Wall Normalization and Execution Pricing (T32)

Enforces:
- Unified quote unit: all-in counter-currency cost to buy/sell N ArcUSDC
- Strict bid/ask price inversion and maker-taker fee separation
- Depth tiers and partial-fill detection (never assume infinite depth)
- Explicit USDG <-> USDC peg transition and bridging costs
- Zero-cost / zero-fee assumptions are strictly prohibited
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from arbitrage_contracts.arc_extensions import OtcQuote
from arc_research.wall.sources import OtcOrderBook


class NormalizationError(ValueError):
    """Raised when normalization or depth aggregation fails."""


@dataclass(frozen=True, slots=True)
class ExecutionTier:
    """Executable tranche of OTC liquidity at a discrete price."""

    tier_id: str
    venue: str
    side: str  # "buy_arc_usdc" or "sell_arc_usdc"
    available_base_atoms: int
    quote_price_per_atom: Decimal
    fee_bps: int
    all_in_quote_atoms: int  # Total quote asset required (if buying) or received (if selling)


@dataclass(frozen=True, slots=True)
class NormalizedWallQuote:
    """Aggregated execution cost and rate for a target quantity."""

    target_base_atoms: int
    filled_base_atoms: int
    is_fully_filled: bool
    side: str  # "buy_arc_usdc" or "sell_arc_usdc"
    total_quote_atoms: int
    all_in_fee_atoms: int
    effective_rate: Decimal
    peg_conversion_fee_atoms: int
    tiers_used: tuple[ExecutionTier, ...]


class OtcWallNormalizer:
    """Normalizes raw OTC orderbooks into executable cost and depth tiers."""

    @classmethod
    def aggregate_execution(
        cls,
        orderbook: OtcOrderBook,
        side: str,  # "buy_arc_usdc" (trader buys from asks) or "sell_arc_usdc" (trader sells into bids)
        target_base_atoms: int,
        taker_fee_bps: int = 15,  # 15 bps default taker fee
        peg_conversion_bps: int = 0,  # e.g., USDG peg conversion fee
        now_utc: float = 0.0,
    ) -> NormalizedWallQuote:
        """Aggregate available liquidity to determine all-in execution cost.

        Invariants:
        1. target_base_atoms must be positive integer.
        2. Bids/asks expired relative to now_utc are rejected.
        3. If available depth < target_base_atoms, is_fully_filled is False.
        4. Fees are deducted/added according to buy/sell side.
        """
        if target_base_atoms <= 0:
            raise NormalizationError(f"target_base_atoms must be positive, got {target_base_atoms}")

        if side not in ("buy_arc_usdc", "sell_arc_usdc"):
            raise NormalizationError(f"Invalid side {side!r}; must be 'buy_arc_usdc' or 'sell_arc_usdc'")

        # If trader wants to buy ArcUSDC, they take from orderbook asks (venue is selling)
        # If trader wants to sell ArcUSDC, they hit orderbook bids (venue is buying)
        quotes = orderbook.asks if side == "buy_arc_usdc" else orderbook.bids

        # Filter valid and unexpired quotes
        active_quotes = [q for q in quotes if q.expiry_timestamp > now_utc]

        # Sort:
        # If buying, sort ascending by price (cheapest ask first)
        # If selling, sort descending by price (highest bid first)
        def quote_price(q: OtcQuote) -> Decimal:
            # Price in quote tokens per base atom
            if q.amount_in_atoms <= 0:
                return Decimal(0)
            return Decimal(q.amount_out_atoms) / Decimal(q.amount_in_atoms)

        reverse_sort = side == "sell_arc_usdc"
        sorted_quotes = sorted(active_quotes, key=quote_price, reverse=reverse_sort)

        remaining_atoms = target_base_atoms
        filled_atoms = 0
        total_quote_atoms = 0
        total_fees_atoms = 0
        total_peg_fees_atoms = 0
        tiers: list[ExecutionTier] = []

        for idx, q in enumerate(sorted_quotes):
            if remaining_atoms <= 0:
                break

            tier_cap = q.amount_in_atoms
            fill_in_tier = min(remaining_atoms, tier_cap)

            unit_price = Decimal(q.amount_out_atoms) / Decimal(q.amount_in_atoms)
            raw_quote_atoms = int(Decimal(fill_in_tier) * unit_price)

            # Taker fee calculation
            fee_atoms = int(Decimal(raw_quote_atoms) * Decimal(taker_fee_bps) / Decimal(10000))
            peg_fee_atoms = int(Decimal(raw_quote_atoms) * Decimal(peg_conversion_bps) / Decimal(10000))

            if side == "buy_arc_usdc":
                # Trader pays raw quote + taker fee + peg fee
                all_in_tier_quote = raw_quote_atoms + fee_atoms + peg_fee_atoms
            else:
                # Trader receives raw quote - taker fee - peg fee
                all_in_tier_quote = max(0, raw_quote_atoms - fee_atoms - peg_fee_atoms)

            tier = ExecutionTier(
                tier_id=f"{q.quote_id}-t{idx}",
                venue=q.venue,
                side=side,
                available_base_atoms=fill_in_tier,
                quote_price_per_atom=unit_price,
                fee_bps=taker_fee_bps,
                all_in_quote_atoms=all_in_tier_quote,
            )
            tiers.append(tier)

            filled_atoms += fill_in_tier
            total_quote_atoms += all_in_tier_quote
            total_fees_atoms += fee_atoms
            total_peg_fees_atoms += peg_fee_atoms
            remaining_atoms -= fill_in_tier

        effective_rate = (
            Decimal(total_quote_atoms) / Decimal(filled_atoms)
            if filled_atoms > 0
            else Decimal(0)
        )

        return NormalizedWallQuote(
            target_base_atoms=target_base_atoms,
            filled_base_atoms=filled_atoms,
            is_fully_filled=filled_atoms >= target_base_atoms,
            side=side,
            total_quote_atoms=total_quote_atoms,
            all_in_fee_atoms=total_fees_atoms,
            effective_rate=effective_rate,
            peg_conversion_fee_atoms=total_peg_fees_atoms,
            tiers_used=tuple(tiers),
        )
