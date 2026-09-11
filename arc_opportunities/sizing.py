"""Arc Bounded Trade Sizing and Grid Search Engine (T22)

Enforces:
- Hard per-transaction limit <= 500 USD (strictly enforced, raises on excess)
- Discrete integer atom conversions without float coercion
- Bounded grid search (e.g. 10, 50, 100, 250, 500 USD) with optional local bisection
- Complete reporting of negative and rejected sizes for statistical auditing
- No false claims of 'globally optimal'; reports best tested within search budget
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arbitrage_contracts.arc_extensions import CostEvidence
from arbitrage_contracts.identity import Amount, AssetRef
from arbitrage_contracts.quote import (
    QuoteEvidence,
    QuoteStatus,
    RouteRef,
)
from arc_opportunities.costs import ArcCostBreakdown
from arc_opportunities.economics import (
    ArcEconomicEvaluator,
    EconomicsError,
    validate_positive_decimal,
)
from arc_opportunities.quote_bridge import ArcQuoteBridge
from state_graph.types import FrozenEpoch

MAX_HARD_TRADE_USD = Decimal("500.0")
DEFAULT_GRID_USD = (
    Decimal("10.0"),
    Decimal("50.0"),
    Decimal("100.0"),
    Decimal("250.0"),
    Decimal("500.0"),
)


class SizingError(ValueError):
    """Raised for sizing policy violations or excessive amount requests."""


@dataclass(frozen=True, slots=True)
class EvaluatedSize:
    """Outcome for a specific tested input size."""

    amount_in: Amount
    amount_usd: Decimal
    quote: QuoteEvidence
    breakdown: ArcCostBreakdown
    net_atoms: int | None
    net_usd: Decimal | None
    is_profitable: bool
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Result of bounded grid trade size search."""

    route_id: str
    best_size: EvaluatedSize | None
    evaluated_sizes: tuple[EvaluatedSize, ...]
    max_tested_usd: Decimal
    rejection_reasons: tuple[str, ...]
    search_completed: bool


class ArcTradeSizeOptimizer:
    """Bounded trade sizing optimizer for Arc closed routes."""

    def __init__(
        self,
        bridge: ArcQuoteBridge,
        evaluator: ArcEconomicEvaluator,
        grid_usd: Sequence[Decimal] = DEFAULT_GRID_USD,
        max_trade_usd: Decimal = MAX_HARD_TRADE_USD,
        refinement_steps: int = 2,
    ) -> None:
        self.bridge = bridge
        self.evaluator = evaluator
        self.max_trade_usd = validate_positive_decimal(max_trade_usd, "max_trade_usd")
        if self.max_trade_usd > MAX_HARD_TRADE_USD:
            raise SizingError(f"max_trade_usd (${self.max_trade_usd}) exceeds hard limit (${MAX_HARD_TRADE_USD})")

        validated_grid: list[Decimal] = []
        for g in grid_usd:
            g_dec = validate_positive_decimal(g, "grid_usd_item")
            if g_dec > self.max_trade_usd:
                raise SizingError(f"Grid item ${g_dec} exceeds max trade limit ${self.max_trade_usd}")
            validated_grid.append(g_dec)
        self.grid_usd = tuple(sorted(set(validated_grid)))
        self.refinement_steps = refinement_steps

    def search_size(
        self,
        route: RouteRef,
        epoch: FrozenEpoch,
        base_asset_usd_price: Decimal | str,
        gas_evidence: CostEvidence | None = None,
        otc_evidence: CostEvidence | None = None,
        token_decimals: Mapping[Any, int] | None = None,
    ) -> SizingResult:
        """Evaluate route across discrete grid sizes and refine best profitable bracket."""
        price_dec = validate_positive_decimal(base_asset_usd_price, "base_asset_usd_price")
        base_asset = route.base_asset
        # Determine base decimals
        base_decimals = 18
        if token_decimals and base_asset in token_decimals:
            base_decimals = token_decimals[base_asset]
        scale = Decimal(10**base_decimals)

        tested: list[EvaluatedSize] = []
        reasons: list[str] = []

        # 1. Evaluate coarse grid
        for usd_amt in self.grid_usd:
            # Convert USD to atoms: atoms = int((usd / price) * 10^decimals)
            atoms = int((usd_amt / price_dec * scale).to_integral_value())
            if atoms <= 0:
                continue

            amount_in = Amount(asset_ref=base_asset, atoms=atoms, decimals=base_decimals)
            eval_size = self._evaluate_single_size(
                route=route,
                amount_in=amount_in,
                amount_usd=usd_amt,
                epoch=epoch,
                price_dec=price_dec,
                gas_evidence=gas_evidence,
                otc_evidence=otc_evidence,
                token_decimals=token_decimals,
            )
            tested.append(eval_size)
            if eval_size.rejection_reason:
                reasons.append(eval_size.rejection_reason)

        # 2. Find best profitable size from tested
        profitable_sizes = [t for t in tested if t.is_profitable and t.net_atoms is not None and t.net_atoms > 0]

        best: EvaluatedSize | None = None
        if profitable_sizes:
            best = max(profitable_sizes, key=lambda t: t.net_atoms or 0)

            # 3. Optional local refinement between best and adjacent points
            if self.refinement_steps > 0:
                best_idx = tested.index(best)
                lower_usd = tested[best_idx - 1].amount_usd if best_idx > 0 else best.amount_usd / 2
                upper_usd = tested[best_idx + 1].amount_usd if best_idx < len(tested) - 1 else best.amount_usd

                curr_lower = lower_usd
                curr_upper = min(upper_usd, self.max_trade_usd)

                for _ in range(self.refinement_steps):
                    mid_usd = (curr_lower + curr_upper) / 2
                    mid_atoms = int((mid_usd / price_dec * scale).to_integral_value())
                    if mid_atoms <= 0:
                        break
                    mid_amt = Amount(asset_ref=base_asset, atoms=mid_atoms, decimals=base_decimals)
                    mid_eval = self._evaluate_single_size(
                        route=route,
                        amount_in=mid_amt,
                        amount_usd=mid_usd,
                        epoch=epoch,
                        price_dec=price_dec,
                        gas_evidence=gas_evidence,
                        otc_evidence=otc_evidence,
                        token_decimals=token_decimals,
                    )
                    tested.append(mid_eval)
                    if mid_eval.is_profitable and (best is None or (mid_eval.net_atoms or 0) > (best.net_atoms or 0)):
                        best = mid_eval
                        curr_lower = mid_usd
                    else:
                        curr_upper = mid_usd

        # Deduplicate and sort evaluated sizes by amount_usd
        sorted_tested = tuple(sorted(tested, key=lambda t: t.amount_usd))

        return SizingResult(
            route_id=route.route_id,
            best_size=best,
            evaluated_sizes=sorted_tested,
            max_tested_usd=max(t.amount_usd for t in sorted_tested) if sorted_tested else Decimal("0"),
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            search_completed=True,
        )

    def _evaluate_single_size(
        self,
        route: RouteRef,
        amount_in: Amount,
        amount_usd: Decimal,
        epoch: FrozenEpoch,
        price_dec: Decimal,
        gas_evidence: CostEvidence | None,
        otc_evidence: CostEvidence | None,
        token_decimals: Mapping[Any, int] | None,
    ) -> EvaluatedSize:
        quote = self.bridge.quote_exact_input(
            route=route,
            amount_in=amount_in,
            epoch=epoch,
            token_decimals=token_decimals,
        )

        breakdown = self.evaluator.evaluate_quote(
            quote=quote,
            gas_evidence=gas_evidence,
            otc_evidence=otc_evidence,
            base_asset_usd_price=price_dec,
        )

        rejection: str | None = None
        is_profitable = False

        if quote.status == QuoteStatus.UNSUPPORTED:
            rejection = f"Quote UNSUPPORTED: {quote.error}"
        elif breakdown.economic_status == "unknown":
            rejection = "Economic status UNKNOWN (missing Gas or valuation evidence)"
        elif breakdown.net_atoms is not None and breakdown.net_atoms <= 0:
            rejection = f"Unprofitable: net_atoms={breakdown.net_atoms} <= 0"
        elif breakdown.economic_status == "profitable" and breakdown.net_atoms is not None and breakdown.net_atoms > 0:
            is_profitable = True

        return EvaluatedSize(
            amount_in=amount_in,
            amount_usd=amount_usd,
            quote=quote,
            breakdown=breakdown,
            net_atoms=breakdown.net_atoms,
            net_usd=breakdown.net_usd,
            is_profitable=is_profitable,
            rejection_reason=rejection,
        )
