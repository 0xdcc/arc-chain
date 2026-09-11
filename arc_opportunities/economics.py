"""Arc Unified Economic Evaluation and Accounting Engine (T20)

Enforces:
- Single-deduction defense: if fee_included == YES, DEX fees are NOT deducted twice
- Fail-closed UNKNOWN propagation: missing Gas or valuation leaves net_atoms=None, NOT 0
- Single balance domain for Arc USDC (native 18-dec and ERC20 6-dec share domain, no free swaps)
- Pure integer atoms and Decimal arithmetic for zero floating-point drift
- Strict output_floor guarantee: output_floor >= amount_in + ceil(gas_atoms) + 1 atom
- Separation of on-chain atom ledger and external OTC financing costs
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from arbitrage_contracts.arc_extensions import CostEvidence
from arbitrage_contracts.quote import (
    QuoteEvidence,
    QuoteStatus,
    TriState,
)
from arc_opportunities.cost_units import cost_in_base_atoms
from arc_opportunities.costs import (
    ArcCostBreakdown,
)

USDC_SHARED_BALANCE_DOMAIN = "usdc_shared"


class EconomicsError(ValueError):
    """Base error for Arc economic evaluation violations."""


def validate_positive_decimal(value: Any, name: str) -> Decimal:
    """Validate that value is a strictly positive, finite Decimal without float coercion."""
    if isinstance(value, (bool, float)):
        raise EconomicsError(f"{name} must not be boolean or float; pass Decimal, int, or string")
    try:
        dec = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError) as exc:
        raise EconomicsError(f"Invalid decimal for {name}: {value!r}") from exc
    if not dec.is_finite() or dec <= Decimal("0"):
        raise EconomicsError(f"{name} must be finite and strictly positive, got: {dec}")
    return dec


class ArcEconomicEvaluator:
    """Economic Evaluator for Arc Opportunities."""

    def __init__(
        self, default_gas_payer: str | None = None, default_beneficiary: str | None = None
    ) -> None:
        self.default_gas_payer = default_gas_payer
        self.default_beneficiary = default_beneficiary

    def evaluate_quote(
        self,
        quote: QuoteEvidence,
        gas_evidence: CostEvidence | None = None,
        otc_evidence: CostEvidence | None = None,
        base_asset_usd_price: Decimal | str | None = None,
        slippage_bps: int = 50,
    ) -> ArcCostBreakdown:
        """Evaluate net economic profit on an Arc quote evidence.

        Invariants:
        1. If quote is not QUOTED or delta_atoms is None, returns unknown.
        2. If gas_evidence is None, net_atoms is None and status is "unknown" (UNKNOWN is NOT 0).
        3. If fee_included == TriState.YES, DEX fee is ALREADY deducted; do NOT deduct again.
        4. Strict output floor: expected_out must cover amount_in + gas_atoms + 1 atom.
        5. Native vs ERC20 USDC shared balance domain: cannot be used for synthetic zero-cost profit.
        """
        if type(slippage_bps) is not int or not 0 <= slippage_bps < 10000:
            raise EconomicsError("slippage_bps must be an integer in [0, 10000)")
        if quote.amount_in.asset_ref != quote.route_ref.base_asset:
            raise EconomicsError("Input asset must match route base asset")
        if quote.amount_out is not None:
            if (
                quote.amount_out.asset_ref != quote.amount_in.asset_ref
                or quote.amount_out.decimals != quote.amount_in.decimals
            ):
                raise EconomicsError("Closed-loop output asset/decimals mismatch")
            if quote.delta_atoms is not None and (
                quote.delta_atoms != quote.amount_out.atoms - quote.amount_in.atoms
            ):
                raise EconomicsError("Quote delta does not match actual amounts")
        # 1. Basic quote validation
        if (
            quote.status != QuoteStatus.QUOTED
            or quote.delta_atoms is None
            or quote.amount_out is None
        ):
            return ArcCostBreakdown(
                base_asset=quote.route_ref.base_asset,
                gross_delta_atoms=quote.delta_atoms or 0,
                dex_fee_included_in_quote=(quote.fee_included == TriState.YES),
                gas_cost=gas_evidence,
                otc_cost=otc_evidence,
                net_atoms=None,
                economic_status="unknown",
                calculation_notes=("Quote status is not QUOTED or delta is None",),
            )

        # 2. Check for shared USDC balance domain violation in route
        hops = quote.route_ref.hops
        for hop in hops:
            # Detect if both assets claim USDC identity but attempt synthetic edge
            in_dom = getattr(hop.asset_in, "balance_domain_id", None)
            out_dom = getattr(hop.asset_out, "balance_domain_id", None)
            if in_dom == USDC_SHARED_BALANCE_DOMAIN and out_dom == USDC_SHARED_BALANCE_DOMAIN:
                # Same underlying balance domain cannot produce free arbitrage
                if hop.pool_key.protocol_id == "synthetic_wrapper":
                    return ArcCostBreakdown(
                        base_asset=quote.route_ref.base_asset,
                        gross_delta_atoms=quote.delta_atoms,
                        dex_fee_included_in_quote=(quote.fee_included == TriState.YES),
                        gas_cost=gas_evidence,
                        otc_cost=otc_evidence,
                        net_atoms=None,
                        economic_status="unknown",
                        calculation_notes=(
                            "Rejected synthetic conversion between same balance domain views",
                        ),
                    )

        notes: list[str] = []
        dex_fee_included = quote.fee_included == TriState.YES
        if dex_fee_included:
            notes.append("DEX fees already included in quote delta; single deduction preserved")
        else:
            notes.append("DEX fee inclusion unknown; raw spread only")

        # 3. Gas Evidence Check: UNKNOWN is NOT 0!
        if gas_evidence is None:
            return ArcCostBreakdown(
                base_asset=quote.route_ref.base_asset,
                gross_delta_atoms=quote.delta_atoms,
                dex_fee_included_in_quote=dex_fee_included,
                gas_cost=None,
                otc_cost=otc_evidence,
                net_atoms=None,
                economic_status="unknown",
                calculation_notes=tuple(
                    notes + ["Gas evidence missing; net profit UNKNOWN (cannot assume 0)"]
                ),
            )

        if quote.fee_included != TriState.YES or quote.impact_included != TriState.YES:
            return ArcCostBreakdown(
                base_asset=quote.route_ref.base_asset,
                gross_delta_atoms=quote.delta_atoms,
                dex_fee_included_in_quote=dex_fee_included,
                gas_cost=gas_evidence,
                otc_cost=otc_evidence,
                net_atoms=None,
                economic_status="unknown",
                calculation_notes=tuple(notes + ["DEX fee or price impact inclusion UNKNOWN"]),
            )
        try:
            gas_atoms = cost_in_base_atoms(
                gas_evidence,
                quote.amount_in.asset_ref,
                quote.amount_in.decimals,
                quote.state_version_ref,
                quote.data_mode,
                "gas",
            )
            otc_atoms = 0
            if otc_evidence is not None:
                otc_atoms = cost_in_base_atoms(
                    otc_evidence,
                    quote.amount_in.asset_ref,
                    quote.amount_in.decimals,
                    quote.state_version_ref,
                    quote.data_mode,
                    "otc_channel",
                )
                notes.append(f"Deducted allocated external OTC cost: {otc_atoms} base atoms")
        except ValueError as exc:
            return ArcCostBreakdown(
                base_asset=quote.route_ref.base_asset,
                gross_delta_atoms=quote.delta_atoms,
                dex_fee_included_in_quote=dex_fee_included,
                gas_cost=gas_evidence,
                otc_cost=otc_evidence,
                net_atoms=None,
                economic_status="unknown",
                calculation_notes=tuple(notes + [str(exc)]),
            )
        gross_delta = quote.delta_atoms
        total_costs_atoms = gas_atoms + otc_atoms

        net_atoms = gross_delta - total_costs_atoms
        status = "profitable" if net_atoms > 0 else "unprofitable"

        # 4. Price & USD valuation
        net_usd: Decimal | None = None
        if base_asset_usd_price is not None:
            price_dec = validate_positive_decimal(base_asset_usd_price, "base_asset_usd_price")
            decimals = quote.amount_in.decimals
            scale = Decimal(10**decimals)
            net_usd = (Decimal(net_atoms) * price_dec) / scale

        # 5. Output floor check for strict profitability
        # In strict profitable mode: expected_out must cover amount_in + gas_atoms + 1
        amount_in_atoms = quote.amount_in.atoms
        expected_out_atoms = quote.amount_out.atoms
        required_floor = amount_in_atoms + gas_atoms + 1
        slippage_min = (expected_out_atoms * (10000 - slippage_bps)) // 10000
        output_floor = max(slippage_min, required_floor)

        if expected_out_atoms < output_floor:
            notes.append(
                f"Expected out {expected_out_atoms} below strict output floor {output_floor}"
            )
            if status == "profitable":
                status = "unprofitable"

        payer = gas_evidence.gas_payer or self.default_gas_payer
        beneficiary = gas_evidence.beneficiary or self.default_beneficiary

        return ArcCostBreakdown(
            base_asset=quote.route_ref.base_asset,
            gross_delta_atoms=gross_delta,
            dex_fee_included_in_quote=dex_fee_included,
            gas_cost=gas_evidence,
            otc_cost=otc_evidence,
            gas_payer=payer,
            beneficiary=beneficiary,
            net_atoms=net_atoms,
            economic_status=status,
            net_usd=net_usd,
            calculation_notes=tuple(notes),
        )
